from __future__ import annotations

import asyncio, io, json, os, re, time

from astrbot.api import AstrBotConfig, logger
from astrbot.api import message_components as Comp
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register

from .client import CbgClientConfig, CbgClientError, MobileAuthRequired, YjCbgClient
from .formatters import (
    format_help,
    format_lowest_price,
    format_search_result,
    parse_price_range,
    parse_star_position_ranges,
    _matches_price_range,
    _ordersn,
    _pick,
    _price_fen,
    _variation,
    _build_link,
)


@register(
    "astrbot_plugin_yjcbg",
    "Codex",
    "查询永劫无间藏宝阁账号/商品数据。",
    "0.2.0",
)
class YjCbgPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self.client = YjCbgClient(
            CbgClientConfig(
                base_url=str(self.config.get("base_url", "https://yjwujian.cbg.163.com")),
                api_prefix=str(self.config.get("api_prefix", "/cgi/api")),
                cookie=str(self.config.get("cookie", "") or ""),
                timeout_seconds=int(self.config.get("timeout_seconds", 12)),
                request_interval=float(self.config.get("request_interval", 0.5)),
            )
        )
        self._reminder_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reminders.json")
        self._reminders = self._load_reminders()
        self._reminder_interval = max(15, int(self.config.get("reminder_interval", 60)))
        self._reminder_task = None
        self._pending_auth: dict[str, dict] = {}
        self._favorite_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "favorites.json")
        self._favorites = self._load_favorites()
        self._start_reminder_task()

    @filter.command("永劫藏宝阁", alias={"藏宝阁", "永劫cbg", "yjcbg"})
    async def yjcbg(self, event: AstrMessageEvent):
        """永劫无间藏宝阁查询。"""
        self._start_reminder_task()
        args = _split_args(event.message_str)
        if not args:
            yield event.plain_result(format_help())
            return

        user_id = event.get_sender_id() or ""
        command = args[0].lower()

        # 若用户有未完成的短信验证，且本次消息是验证码（裸数字或"验证码 xxx"），则完成验证后重跑原操作
        pending = self._pending_auth.get(user_id)
        if pending and pending.get("pending"):
            sms_code = None
            if command in {"验证码", "code", "sms"} and len(args) >= 2:
                sms_code = args[1].strip()
            elif re.fullmatch(r"\d{4,8}", args[0]):
                sms_code = args[0].strip()
            if sms_code:
                result = await self._complete_sms_auth(event, sms_code, pending)
                yield event.plain_result(result)
                return

        try:
            if command in {"help", "帮助", "-h", "--help"}:
                yield event.plain_result(format_help())
            elif command in {"search", "s", "搜索", "查"}:
                _kw = " ".join(args[1:]).strip() or "全部"
                yield event.plain_result(await self._handle_search(event, args[1:]))
            elif command in {"提醒", "remind", "watch"}:
                yield event.plain_result(await self._handle_remind(event, args[1:]))
            elif command in {"取消提醒", "取消", "unwatch"}:
                yield event.plain_result(await self._handle_cancel_remind(event, args[1:]))
            elif command in {"提醒列表", "watchlist", "reminders"}:
                yield event.plain_result(self._handle_remind_list(event))
            elif command in {"最低价", "低价", "cheapest", "min"}:
                yield event.plain_result(await self._handle_lowest_price(event, args[1:]))
            elif command in {"收藏", "fav", "favorite"}:
                yield event.plain_result(await self._handle_fav(event, args[1:]))
            elif command in {"取消收藏", "unfav"}:
                yield event.plain_result(await self._handle_unfav(event, args[1:]))
            elif command in {"收藏列表", "favlist"}:
                yield event.plain_result(self._handle_fav_list(event))
            elif command in {"刷新收藏", "refreshfav"}:
                yield event.plain_result(await self._handle_refresh_fav(event))
            elif command in {"诊断", "diag", "check"}:
                yield event.plain_result(await self._handle_diag(event, args[1:]))
            else:
                if command in {"验证码", "code", "sms"}:
                    yield event.plain_result("当前没有待验证的短信验证码。请先触发一次搜索。")
                    return
                keyword = " ".join(args).strip()
                yield event.plain_result(await self._handle_search(event, [keyword]))
        except MobileAuthRequired as exc:
            yield event.plain_result(await self._initiate_sms_auth(event, exc))
        except CbgClientError as exc:
            logger.exception("yjcbg query failed: %s", exc)
            yield event.plain_result(self._format_error(exc))
        except Exception as exc:
            logger.exception("yjcbg unexpected error")
            yield event.plain_result(self._format_error(exc))

    async def _handle_search(self, event, args: list[str]) -> str:
        if not args:
            return "请提供搜索关键词，例如：/永劫藏宝阁 搜索 胡桃"
        page = 1
        if args[-1].isdigit():
            page = max(1, int(args.pop()))
        star_ranges = None
        fair_show_filter = None
        price_range = None
        keyword = " ".join(args).strip()
        _star_ranges, keyword = parse_star_position_ranges(keyword)
        if _star_ranges:
            star_ranges = _star_ranges
        _price_range, keyword = parse_price_range(keyword)
        if _price_range:
            price_range = _price_range
        for term in ("公示期", "在售期", "公示", "在售"):
            if term in keyword:
                idx = keyword.index(term)
                if term in ("公示期", "公示"):
                    fair_show_filter = 0
                else:
                    fair_show_filter = 1
                keyword = keyword[:idx].strip() + " " + keyword[idx + len(term):].strip()
                keyword = keyword.strip()
                break
        sort_order = None
        for key, term in [("price_asc", "价格升"), ("price_desc", "价格降"), ("collect_asc", "收藏升"), ("collect_desc", "收藏降")]:
            if term in keyword:
                sort_order = key
                idx = keyword.index(term)
                keyword = keyword[:idx].strip() + " " + keyword[idx + len(term):].strip()
                keyword = keyword.strip()
                break
        _umo = getattr(event, "unified_msg_origin", None)
        async def _progress_cb(text: str) -> None:
            if not _umo:
                return
            try:
                await self.context.send_message(_umo, MessageChain([Comp.Plain(text)]))
            except Exception:
                pass

        progress_cb = _progress_cb
        max_results = max(1, int(self.config.get("max_results", 10)))
        is_id_search = re.match(r'^[a-zA-Z]\d+$', keyword.strip()) is not None
        id_token = _extract_id_token(keyword)
        if id_token and keyword.strip().lower() != id_token.lower():
            _search_type = "combined"
        elif is_id_search:
            _search_type = "id"
        elif star_ranges:
            _search_type = "star"
        else:
            _search_type = "skin"
        # 查询开始仅提示一次，不显示预计耗时
        _prompt = f"⏳ 正在查询：{keyword}"
        if _search_type == "star":
            _prompt += "（星格全遍历，耗时较长）"
        elif price_range:
            _prompt += "（价格区间全遍历，耗时较长）"
        _prompt += "，请稍候……"
        try:
            await progress_cb(_prompt)
        except Exception:
            pass
        if _search_type == "combined":
            skin_name = keyword.replace(id_token, "").strip()
            data = await self._search_by_skin_and_id(skin_name, id_token, max_results, progress_cb=progress_cb)
        elif _search_type == "id":
            data = await self._search_by_id(keyword, page, max_results, progress_cb=progress_cb)
        else:
            data = await self._search_by_skin_type(keyword, page, max_results, sort_order, star_ranges, price_range, fair_show_filter, progress_cb=progress_cb)
        items = (data.get("result") or [])[:max_results]
        if not items:
            # 空结果时记录详细日志，便于定位是接口无数据、风控还是结构变化
            logger.warning(
                "yjcbg search empty: keyword=%r type=%s result_len=%s pager=%s is_id=%s id_token=%r",
                keyword,
                "skin_and_id" if (id_token and keyword.strip().lower() != id_token.lower()) else ("id" if is_id_search else "skin_type"),
                len(data.get("result") or []),
                data.get("pager"),
                is_id_search,
                id_token,
            )
        if items:
            await self._enrich_items(items)
        return format_search_result(data, max_results, page, keyword, star_ranges, fair_show_filter, sort_order, price_range=price_range)

    async def _initiate_sms_auth(self, event, exc: MobileAuthRequired) -> str:
        """触发 MOBILE_AUTH 时自动发送短信验证码，并登记待验证状态。"""
        user_id = event.get_sender_id() or ""
        op_type = exc.op_type or "general_check"
        op_id = exc.op_id or "general_check"
        try:
            await self.client.get_sms_code(op_type=op_type, op_id=op_id)
        except CbgClientError as send_exc:
            logger.exception("yjcbg send sms code failed: %s", send_exc)
            return self._format_error(send_exc)
        # 记录触发验证时的搜索参数，验证通过后自动重跑
        search_args: list[str] = []
        parts = _split_args(event.message_str or "")
        if parts:
            cmd0 = parts[0].lower()
            if cmd0 in {"search", "s", "搜索", "查"}:
                search_args = parts[1:]
        self._pending_auth[user_id] = {
            "pending": True,
            "op_type": op_type,
            "op_id": op_id,
            "search_args": search_args,
            "created_at": time.time(),
        }
        logger.info(
            "yjcbg sms auth initiated: user=%s op_type=%s op_id=%s",
            user_id, op_type, op_id,
        )
        return (
            "藏宝阁要求手机短信验证。验证码已发送到绑定的手机号，请查收。\n"
            "请回复验证码完成验证（例如：/永劫藏宝阁 验证码 123456，或直接发送验证码数字）。"
        )

    async def _complete_sms_auth(self, event, sms_code: str, pending: dict) -> str:
        """用户输入验证码后校验，成功后自动重跑原搜索。"""
        user_id = event.get_sender_id() or ""
        op_type = pending.get("op_type") or "general_check"
        op_id = pending.get("op_id") or "general_check"
        try:
            await self.client.verify_sms_code(sms_code, op_type=op_type, op_id=op_id)
        except CbgClientError as exc:
            logger.exception("yjcbg verify sms code failed: %s", exc)
            return self._format_error(exc)
        # 验证成功，清除待验证状态
        self._pending_auth.pop(user_id, None)
        logger.info("yjcbg sms auth passed: user=%s", user_id)
        # 验证通过后，自动重跑触发验证时的那次搜索
        search_args = pending.get("search_args") or []
        if search_args:
            return "✅ 手机验证通过。正在重新查询，请稍候……\n\n" + await self._handle_search(event, list(search_args))
        return "✅ 手机验证通过。请重新发送您的查询指令。"

    def _estimate_scan_seconds(self, total_pages: int) -> int:
        """估算遍历全部页所需的秒数（基于并发、限流间隔、批间等待等配置）。"""
        if not total_pages or total_pages <= 0:
            return 0
        concurrency = max(1, int(self.config.get("request_concurrency", 2)))
        interval = max(0.1, float(self.config.get("id_scan_interval", 0.6)))
        batch_pages = max(1, int(self.config.get("batch_pages", 10)))
        batch_interval = max(0.0, float(self.config.get("batch_interval", 5)))
        request_interval = max(0.0, float(self.config.get("request_interval", 0.5)))
        # 每页请求耗时约 = 全局限流间隔 + 网络往返（估算 0.8 秒）
        page_time = request_interval + 0.8
        # 批数
        batches = (total_pages + batch_pages - 1) // batch_pages
        # 每批耗时 ≈ 该批页数按并发抓取的时间 + 批间等待
        per_batch = ((batch_pages + concurrency - 1) // concurrency) * page_time + interval * (batch_pages // concurrency) + batch_interval
        total = batches * per_batch
        return max(1, int(total))

    async def _estimate_search_seconds(self, search_type: str, keyword: str = "", star_ranges=None) -> int:
        """估算不同搜索类型的预计耗时（秒）。

        search_type: star / skin / id / combined
        基于配置的并发、限流间隔、批间等待、每页网络耗时等粗略估算。
        """
        request_interval = max(0.0, float(self.config.get("request_interval", 0.5)))
        page_time = request_interval + 0.8  # 每页请求耗时（限流 + 网络往返）
        concurrency = max(1, int(self.config.get("request_concurrency", 2)))
        interval = max(0.1, float(self.config.get("id_scan_interval", 0.6)))
        batch_pages = max(1, int(self.config.get("batch_pages", 10)))
        batch_interval = max(0.0, float(self.config.get("batch_interval", 5)))
        scan_pages = max(0, int(self.config.get("id_scan_pages", 0)))
        if search_type == "star":
            if star_ranges:
                _tp = await self._probe_total_pages(keyword)
                if _tp:
                    return self._estimate_scan_seconds(_tp)
            return 0  # 未探测到，不估算
        # 皮肤类型列表抓取请求数（4 个 kindid × 平均 2 页）
        type_list_reqs = 8
        if search_type == "skin":
            # 普通搜索：类型列表 + 1 页商品
            return max(1, int((type_list_reqs + 1) * page_time))
        if search_type == "id":
            # 编号搜索：类型列表 + 命中前平均扫描约 40 个类型 × 每个深度页
            depth = scan_pages if scan_pages > 0 else 10
            avg_types = 40
            return max(1, int((type_list_reqs + avg_types * depth) * page_time))
        if search_type == "combined":
            # 组合搜索：类型列表 + 匹配类型(通常 1-2 个) × 深度页
            depth = scan_pages if scan_pages > 0 else 10
            matched = 2
            return max(1, int((type_list_reqs + matched * depth) * page_time))
        return 0

    async def _probe_total_pages(self, keyword: str) -> int:
        """探测匹配皮肤类型的总页数（用于估算星格全遍历耗时）。"""
        try:
            matched = await self._find_skin_types(keyword)
            if not matched:
                return 0
            equip_type = str(matched[0].get("equip_type"))
            page_size = max(10, int(self.config.get("page_size", 30)))
            data = await self.client.get_equip_list(equip_type, page=1, count=page_size)
            pager = (data or {}).get("pager") or {}
            tp = pager.get("total_pages")
            if isinstance(tp, int) and tp > 0:
                return tp
            return 0
        except Exception:
            return 0

    async def _search_by_skin_type(self, keyword: str, page: int, count: int, sort_order: str | None = None, star_ranges=None, price_range=None, fair_show_filter=None, progress_cb=None) -> dict[str, Any]:
        """2-step search: match skin type first, then get equip list.

        sort_order is passed to the API so the whole dataset is sorted
        (not just the current page).

        When star_ranges or price_range is provided, pages are scanned
        across the skin type and items are filtered by 星格 / 价格 / 公示期
        so results are not limited to the first page only.
        """
        matched_types = await self._find_skin_types(keyword)
        if not matched_types:
            return await self.client.recommend_by_role(keyword, page=page, count=count)
        equip_type = str(matched_types[0].get("equip_type"))
        order_by = _map_sort_order(sort_order)
        if star_ranges or price_range:
            return await self._scan_skin_type_filtered(equip_type, star_ranges, price_range, fair_show_filter, sort_order, progress_cb=progress_cb)
        logger.info("yjcbg: matched '%s' -> equip_type=%s order_by=%s", matched_types[0].get("equip_type_name"), equip_type, order_by)
        return await self.client.get_equip_list(equip_type, page=page, count=count, order_by=order_by)

    async def _scan_skin_type_filtered(self, equip_type: str, star_ranges, price_range=None, fair_show_filter=None, sort_order: str | None = None, progress_cb=None) -> dict[str, Any]:
        """Scan a skin type's pages and collect items matching 星格/价格/公示期.

        star_scan_pages=0 (default) scans all pages (uses pager.total_pages
        as the boundary); >0 limits to the first N pages.
        """
        scan_pages = max(0, int(self.config.get("star_scan_pages", 0)))
        order_by = _map_sort_order(sort_order)

        def _matcher(it):
            return (
                _matches_star_ranges(it, star_ranges)
                and _matches_price_range(it, price_range)
                and (fair_show_filter is None or it.get("pass_fair_show") == fair_show_filter)
            )

        return await self._collect_from_pages(
            equip_type,
            _matcher,
            order_by=order_by,
            scan_pages=scan_pages,
            progress_cb=progress_cb,
        )

    async def _fetch_page(self, equip_type: str, page: int, order_by: str = "") -> dict[str, Any] | None:
        """抓取单个页面，失败返回 None。

        仅当配置的 page_size 大于默认 30 且接口返回空时，才回退到 count=30 重试，
        避免因 page_size 不被支持而搜索不到数据；同时避免超出范围的页被误重试。
        """
        try:
            page_size = max(10, int(self.config.get("page_size", 30)))
            data = await self.client.get_equip_list(equip_type, page=page, count=page_size, order_by=order_by)
            if data and (data.get("result") or data.get("data") or data.get("equip_list")):
                return data
            if page_size > 30:
                # 回退到默认 count=30 再试一次
                data = await self.client.get_equip_list(equip_type, page=page, count=30, order_by=order_by)
                return data
            return data
        except Exception:
            return None

    async def _collect_from_pages(self, equip_type: str, match_func, order_by: str = "", scan_pages: int = 0, stop_after_first: bool = False, progress_cb=None) -> dict[str, Any]:
        """分批抓取某皮肤类型的全部（或前 N 页）并收集匹配项。

        每批查询 batch_pages 页（默认 10 页），批内按 request_concurrency
        并发抓取；每批结束后输出该批页码范围是否有结果，并在批间等待
        batch_interval 秒再开始下一批，以降低触发风控的概率。
        scan_pages=0 表示遍历全部页（用 total_pages 作边界，并辅以
        is_last_page 防止缺失 total_pages 时漏抓）。
        stop_after_first=True 时命中第一个匹配项即返回（用于编号这类唯一值
        的查询，避免无谓遍历剩余页）。
        """
        concurrency = max(1, int(self.config.get("request_concurrency", 2)))
        interval = max(0.1, float(self.config.get("id_scan_interval", 0.6)))
        batch_pages = max(1, int(self.config.get("batch_pages", 10)))
        batch_interval = max(0.0, float(self.config.get("batch_interval", 5)))
        collected = []
        seen = set()
        total_pages = None
        reached_last = False
        empty_streak = 0
        pg = 1
        while True:
            if scan_pages > 0 and pg > scan_pages:
                break
            if total_pages is not None and pg > total_pages:
                break
            if reached_last:
                break
            # 本批页码范围
            batch_start = pg
            batch_end = pg + batch_pages - 1
            if scan_pages > 0:
                batch_end = min(batch_end, scan_pages)
            if total_pages is not None:
                batch_end = min(batch_end, total_pages)
            logger.info(
                "yjcbg collect batch start: equip_type=%s 开始查询 第%d页到第%d页",
                equip_type, batch_start, batch_end,
            )
            any_in_batch = 0
            chunk_start = batch_start
            while chunk_start <= batch_end:
                if reached_last:
                    break
                if total_pages is not None and chunk_start > total_pages:
                    break
                chunk_end = min(chunk_start + concurrency - 1, batch_end)
                if total_pages is not None:
                    chunk_end = min(chunk_end, total_pages)
                chunk = list(range(chunk_start, chunk_end + 1))
                results = await asyncio.gather(
                    *[self._fetch_page(equip_type, p, order_by) for p in chunk]
                )
                for data in results:
                    if not data:
                        continue
                    pager = data.get("pager") or {}
                    if isinstance(pager.get("total_pages"), int) and pager["total_pages"] > 0:
                        total_pages = pager["total_pages"]
                    items = data.get("result") or []
                    if items:
                        any_in_batch += 1
                    for it in items:
                        if match_func(it):
                            key = str(it.get("game_ordersn") or it.get("eid") or id(it))
                            if key not in seen:
                                seen.add(key)
                                collected.append(it)
                            if stop_after_first:
                                return {"result": collected}
                    paging = data.get("paging") or {}
                    # is_last_page 可能在顶层或 paging 中，两者都检查（与 _iter_all_skin_types 一致）
                    if paging.get("is_last_page") or data.get("is_last_page"):
                        reached_last = True
                chunk_start += concurrency
                await asyncio.sleep(interval)
            # 输出本批结果
            if any_in_batch == 0:
                logger.info(
                    "yjcbg collect batch done: equip_type=%s 第%d页到第%d页 无查询结果",
                    equip_type, batch_start, batch_end,
                )
                empty_streak += 1
            else:
                logger.info(
                    "yjcbg collect batch done: equip_type=%s 第%d页到第%d页 有数据，当前累计匹配=%d",
                    equip_type, batch_start, batch_end, len(collected),
                )
                empty_streak = 0
            # 安全兜底：接口未返回 total_pages 且连续多批空时，视为已到末尾。
            # 需要连续 2 批空才停，避免一次限流返回空批就提前中断，漏掉排在后面的商品。
            if total_pages is None and empty_streak >= 2:
                logger.warning(
                    "yjcbg collect stop: equip_type=%s batch=%d-%d empty_streak=%s collected=%d",
                    equip_type, batch_start, batch_end, empty_streak, len(collected),
                )
                break
            # 进度提示仅在查询开始时发送一次，遍历过程不再逐页刷新
            pg = batch_end + 1
            # 批间等待，降低风控
            if not reached_last and not (scan_pages > 0 and pg > scan_pages) and not (total_pages is not None and pg > total_pages):
                logger.info(
                    "yjcbg collect batch sleep: equip_type=%s 等待 %.1f 秒后继续下一批",
                    equip_type, batch_interval,
                )
                await asyncio.sleep(batch_interval)
        logger.info(
            "yjcbg collect done: equip_type=%s pages_scanned_to=%d total_pages=%s collected=%d reached_last=%s",
            equip_type, pg, total_pages, len(collected), reached_last,
        )
        return {"result": collected}

    async def _iter_all_skin_types(self, kindids: list[int] | None = None) -> list[dict[str, Any]]:
        """分页抓取全部类型（英雄3、武器4、宝箱5、礼包6）。

        遍历指定的 kindid 分类（默认覆盖英雄/武器/宝箱/礼包），翻页抓取
        全部皮肤类型，不限于第一页 30 个。这样组合搜索（皮肤名+编号）和编号
        搜索能覆盖所有皮肤变体，不会漏掉未排在前面的类型。
        若配置的 page_size 过大导致接口不返回数据，会回退到默认 count=30。
        接口的 is_last_page 可能位于顶层或 paging 中，两者都检查。
        空页时重试一次，避免接口临时限流导致提前结束漏掉皮肤类型。
        """
        if kindids is None:
            kindids = [3, 4, 5, 6]  # 英雄皮肤 + 武器皮肤 + 宝箱 + 礼包
        collected = []
        seen = set()
        for kindid in kindids:
            page = 1
            max_pages = 60  # 安全上限，防止接口缺分页标记时死循环
            while page <= max_pages:
                page_size = max(10, int(self.config.get("page_size", 30)))
                data = await self.client.get_skin_types(count=page_size, page=page, kindid=kindid)
                items = data.get("equip_type_list") or []
                if not items and page_size > 30:
                    # 回退到默认 count=30 再试一次
                    data = await self.client.get_skin_types(count=30, page=page, kindid=kindid)
                    items = data.get("equip_type_list") or []
                if not items:
                    # 空页重试一次（可能是接口临时限流），仍空才结束
                    await asyncio.sleep(0.5)
                    data = await self.client.get_skin_types(count=30, page=page, kindid=kindid)
                    items = data.get("equip_type_list") or []
                if not items:
                    break
                for st in items:
                    et = str(st.get("equip_type") or "")
                    if et and et not in seen:
                        seen.add(et)
                        collected.append(st)
                # is_last_page 可能在顶层或 paging 中
                paging = data.get("paging") or {}
                if data.get("is_last_page") or paging.get("is_last_page"):
                    break
                page += 1
                await asyncio.sleep(0.1)
        logger.info(
            "yjcbg iter_all_skin_types: kindids=%s total=%d sample=%s",
            kindids, len(collected),
            [str(s.get("equip_type_name") or "") for s in collected[:5]],
        )
        return collected

    async def _find_skin_types(self, keyword: str) -> list[dict[str, Any]]:
        """Return skin types whose name/desc contains keyword (fuzzy)."""
        skin_types = await self._iter_all_skin_types()
        if not skin_types:
            # 兜底：分页遍历可能因接口行为差异取不到，回退到直接取第一页（英雄+武器）
            for _kind in (3, 4, 5, 6):
                skin_data = await self.client.get_skin_types(count=30, kindid=_kind)
                skin_types.extend(skin_data.get("equip_type_list") or [])
        logger.info("yjcbg find_skin_types: keyword=%r total_types=%d", keyword, len(skin_types))
        matched_types = []
        kw_lower = keyword.lower()
        for st in skin_types:
            name = str(st.get("equip_type_name") or "")
            desc = str(st.get("equip_type_desc") or "")
            combined = f"{name} {desc}".lower()
            if kw_lower in combined:
                matched_types.append(st)
            else:
                for part in kw_lower.split():
                    if part and part in combined:
                        matched_types.append(st)
                        break
        logger.info(
            "yjcbg find_skin_types match: keyword=%r matched=%d sample_names=%s",
            keyword, len(matched_types),
            [str(s.get("equip_type_name") or "") for s in matched_types[:5]],
        )
        return matched_types

    async def _search_by_skin_and_id(self, skin_name: str, id_token: str, count: int, progress_cb=None) -> dict[str, Any]:
        """Combined search: skin name + Y number.

        Finds matching skin types by name, then scans each of their equip
        lists for items whose 编号 matches id_token (fuzzy).
        If no skin type matches by name, fall back to scanning ALL skin
        types for the id_token (same as _search_by_id) so the result is
        not empty just because the type-name match failed.
        """
        kw = id_token.strip().lower()
        matched_types = await self._find_skin_types(skin_name)
        logger.info(
            "yjcbg skin_and_id: skin=%r id=%r matched_types=%d",
            skin_name, id_token, len(matched_types),
        )
        collected = []
        seen = set()
        if not matched_types:
            # 兜底：皮肤类型按名称匹配不到，改为扫描全部皮肤类型查找该编号。
            # 注意：Y 编号按类型独立、每个类型都有同号，因此这里只作为名称匹配
            # 完全失败时的兜底（可能返回的是其他类型的同号，结果仅供参考）。
            data = await self._search_by_id(id_token, 1, count, progress_cb=progress_cb)
            for it in (data.get("result") or []):
                key = str(it.get("game_ordersn") or it.get("eid") or id(it))
                if key not in seen:
                    seen.add(key)
                    collected.append(it)
            logger.info(
                "yjcbg skin_and_id fallback: scan_all_types matched=%d",
                len(collected),
            )
            return {"result": collected}
        scan_pages = max(0, int(self.config.get("id_scan_pages", 0)))
        # 带 order_by 拿完整在售列表，否则接口只返回"推荐子集"，会漏掉部分编号
        # （如 Y000060）。网页端按编号排序 attrs.client_show_id ASC 即返回完整列表。
        list_order_by = str(self.config.get("list_order_by", "attrs.client_show_id ASC"))
        for st in matched_types:
            equip_type = str(st.get("equip_type"))
            tname = str(st.get("equip_type_name") or "")
            data = await self._collect_from_pages(
                equip_type,
                lambda it: _item_matches_id(it, kw),
                order_by=list_order_by,
                scan_pages=scan_pages,
                stop_after_first=True,
                progress_cb=progress_cb,
            )
            sub = (data.get("result") or [])
            logger.info(
                "yjcbg skin_and_id scan: type=%r equip_type=%s matched_items=%d",
                tname, equip_type, len(sub),
            )
            for it in sub:
                key = str(it.get("game_ordersn") or it.get("eid") or id(it))
                if key not in seen:
                    seen.add(key)
                    collected.append(it)
        return {"result": collected}

    async def _search_by_id(self, keyword: str, page: int, count: int, progress_cb=None) -> dict[str, Any]:
        """Fuzzy search by Y-number.

        Scans all hero skin types and collects items whose basic_attrs
        (编号) match the keyword (supports partial/fuzzy match).

        No fast path: calling keyword_query directly triggers CBG
        CAPTCHA_AUTH risk control, so we only scan the skin-type lists.
        """
        kw = keyword.strip().lower()
        scan_pages = max(0, int(self.config.get("id_scan_pages", 0)))
        # 必须带 order_by 才能拿到该类型的完整在售列表。不带时接口只返回
        # "推荐子集"，会漏掉部分编号（如 Y000060）。网页端按编号排序
        # (attrs.client_show_id ASC) 即可返回完整列表。
        list_order_by = str(self.config.get("list_order_by", "attrs.client_show_id ASC"))
        skin_types = await self._iter_all_skin_types()
        collected = []
        seen = set()
        for st in skin_types:
            equip_type = str(st.get("equip_type"))
            data = await self._collect_from_pages(
                equip_type,
                lambda it: _item_matches_id(it, kw),
                order_by=list_order_by,
                scan_pages=scan_pages,
                stop_after_first=True,
                progress_cb=progress_cb,
            )
            for it in (data.get("result") or []):
                key = str(it.get("game_ordersn") or it.get("eid") or id(it))
                if key not in seen:
                    seen.add(key)
                    collected.append(it)
            # 编号唯一，命中即停止，不再扫描剩余皮肤类型
            if collected:
                break
        return {"result": collected}

    async def _scan_recommend_by_role(self, role_name: str, match_func) -> dict[str, Any]:
        """扫描 recommend_by_role 的多页，收集匹配项。

        用于皮肤类型按名称匹配不到时的兜底。分页遍历 recommend.py
        返回的商品列表，按 match_func 过滤。
        """
        concurrency = max(1, int(self.config.get("request_concurrency", 3)))
        page_size = max(10, int(self.config.get("page_size", 30)))
        max_pages = 60  # 安全上限，防止接口缺分页标记时死循环
        collected = []
        seen = set()
        total_pages = None
        reached_last = False
        empty_streak = 0
        pg = 1
        while pg <= max_pages:
            if total_pages is not None and pg > total_pages:
                break
            if reached_last:
                break
            batch = list(range(pg, pg + concurrency))
            if total_pages is not None:
                batch = [p for p in batch if p <= total_pages]
            results = await asyncio.gather(
                *[self.client.recommend_by_role(role_name, page=p, count=page_size) for p in batch]
            )
            any_items = 0
            for data in results:
                if not data:
                    continue
                pager = data.get("pager") or {}
                if isinstance(pager.get("total_pages"), int) and pager["total_pages"] > 0:
                    total_pages = pager["total_pages"]
                items = data.get("result") or []
                if items:
                    any_items += 1
                for it in items:
                    if match_func(it):
                        key = str(it.get("game_ordersn") or it.get("eid") or id(it))
                        if key not in seen:
                            seen.add(key)
                            collected.append(it)
                paging = data.get("paging") or {}
                if paging.get("is_last_page") or data.get("is_last_page"):
                    reached_last = True
            if any_items == 0:
                empty_streak += 1
            else:
                empty_streak = 0
            if total_pages is None and empty_streak >= 2:
                break
            pg += concurrency
            await asyncio.sleep(0.3)
        logger.info(
            "yjcbg scan_recommend_by_role: role=%r pages=%d total_pages=%s collected=%d",
            role_name, pg, total_pages, len(collected),
        )
        return {"result": collected}

    async def _handle_remind(self, event, args):
        """注册提醒：某个皮肤出现某个编号时提醒。"""
        if not args:
            return "用法：/永劫藏宝阁 提醒 皮肤名 编号\n例如：/永劫藏宝阁 提醒 风之絮语 Y002481"
        keyword = " ".join(args).strip()
        id_token = _extract_id_token(keyword)
        if not id_token:
            return "请提供 Y 编号：/永劫藏宝阁 提醒 皮肤名 编号"
        skin_name = keyword.replace(id_token, "").strip()
        if not skin_name:
            return "请提供皮肤名：/永劫藏宝阁 提醒 皮肤名 编号"
        # 先验证该皮肤类型存在
        try:
            matched = await self._find_skin_types(skin_name)
        except CbgClientError as exc:
            return self._format_error(exc)
        if not matched:
            return f"找不到皮肤类型：{skin_name}"
        equip_type = str(matched[0].get("equip_type") or "")
        umo = event.unified_msg_origin
        # 去重：同一会话同一皮肤同一编号只保留一个
        self._reminders = [r for r in self._reminders
                           if not (r.get("umo") == umo and r.get("skin") == skin_name and r.get("id") == id_token)]
        self._reminders.append({
            "umo": umo,
            "skin": skin_name,
            "id": id_token,
            "equip_type": equip_type,
            "notified": False,
            "created_at": int(time.time()),
            "sender": event.get_sender_id() or "",
        })
        self._save_reminders()
        return f"已添加提醒：{skin_name} 的编号 {id_token} 出现时，我会通知你。\n当前提醒数：{len(self._reminders)}"

    async def _handle_cancel_remind(self, event, args):
        """取消提醒（按用户区分）。"""
        umo = event.unified_msg_origin
        sender = str(event.get_sender_id() or "")

        def _is_mine(r):
            # 兼容旧数据：无 sender 或 sender 为空时，降级按会话过滤
            r_sender = str(r.get("sender") or "")
            if r_sender:
                return r.get("umo") == umo and r_sender == sender
            return r.get("umo") == umo

        if not args:
            before = len(self._reminders)
            self._reminders = [r for r in self._reminders if not _is_mine(r)]
            removed = before - len(self._reminders)
            self._save_reminders()
            return f"已取消 {removed} 条提醒。"
        keyword = " ".join(args).strip()
        id_token = _extract_id_token(keyword)
        if not id_token:
            return "请提供要取消的编号：/永劫藏宝阁 取消提醒 编号"
        before = len(self._reminders)
        self._reminders = [r for r in self._reminders
                           if not (_is_mine(r) and r.get("id") == id_token)]
        removed = before - len(self._reminders)
        self._save_reminders()
        return f"已取消编号 {id_token} 的 {removed} 条提醒。"
    def _handle_remind_list(self, event):
        """列出当前用户的提醒。"""
        umo = event.unified_msg_origin
        sender = str(event.get_sender_id() or "")
        mine = [r for r in self._reminders
                if (r.get("umo") == umo and
                    (not str(r.get("sender") or "") or str(r.get("sender") or "") == sender))]
        if not mine:
            return "当前没有提醒。可用 /永劫藏宝阁 提醒 皮肤名 编号 添加。"
        lines = ["当前提醒："]
        for i, r in enumerate(mine, 1):
            status = "已触发" if r.get("notified") else "监听中"
            lines.append(f"{i}. {r.get('skin')} | {r.get('id')} | {status}")
        return "\n".join(lines)
    def _start_reminder_task(self):
        if self._reminder_task is None or self._reminder_task.done():
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                # 没有运行中的事件循环：推迟到首个命令时再启动
                self._reminder_task = None
                return
            self._reminder_task = loop.create_task(self._reminder_loop())

    async def _search_id_fast(self, equip_type: str, id_token: str) -> dict[str, Any]:
        """轻量检查：只在指定皮肤类型的最前几页内找编号，用于提醒轮询。

        提醒检查要快速、少请求，避免像完整搜索那样遍历全部皮肤类型和全部页
        而触发风控。默认只扫前 N 页（最新上架），编号出现即可命中。
        """
        kw = id_token.strip().lower()
        scan_pages = max(1, int(self.config.get("reminder_scan_pages", 5)))
        return await self._collect_from_pages(
            equip_type,
            lambda it: _item_matches_id(it, kw),
            scan_pages=scan_pages,
        )

    async def _reminder_loop(self):
        while True:
            try:
                for r in list(self._reminders):
                    if r.get("notified"):
                        continue
                    try:
                        equip_type = r.get("equip_type") or ""
                        if not equip_type:
                            matched = await self._find_skin_types(r["skin"])
                            if not matched:
                                continue
                            equip_type = str(matched[0].get("equip_type") or "")
                            r["equip_type"] = equip_type
                        data = await self._search_id_fast(equip_type, r["id"])
                        items = data.get("result") or []
                        if items:
                            r["notified"] = True
                            self._save_reminders()
                            await self._notify(r, items)
                    except CbgClientError as exc:
                        logger.warning("yjcbg reminder check failed: %s", exc)
                    except Exception as exc:
                        logger.warning("yjcbg reminder check error: %s", exc)
            except Exception as exc:
                logger.warning("yjcbg reminder loop error: %s", exc)
            await asyncio.sleep(self._reminder_interval)

    async def _notify(self, reminder, items):
        try:
            text = f"提醒：{reminder['skin']} 的编号 {reminder['id']} 已出现！"
            sender = str(reminder.get("sender") or "")
            chain = MessageChain()
            if sender:
                try:
                    chain.append(Comp.At(qq=sender))
                except Exception:
                    chain.append(Comp.Plain(f"@{sender} "))
            chain.append(Comp.Plain(text))
            await self.context.send_message(reminder["umo"], chain)
        except Exception as exc:
            logger.warning("yjcbg notify failed: %s", exc)

    def _load_reminders(self):
        try:
            if os.path.exists(self._reminder_file):
                with io.open(self._reminder_file, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    return data
        except Exception as exc:
            logger.warning("yjcbg load reminders failed: %s", exc)
        return []

    def _save_reminders(self):
        try:
            with io.open(self._reminder_file, "w", encoding="utf-8") as f:
                json.dump(self._reminders, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.warning("yjcbg save reminders failed: %s", exc)

    # ---------- 最低价 ----------
    async def _handle_lowest_price(self, event, args: list[str]) -> str:
        if not args:
            return "用法：/永劫藏宝阁 最低价 关键词\n例如：/永劫藏宝阁 最低价 风之絮语"
        keyword = " ".join(args).strip()
        matched = await self._find_skin_types(keyword)
        if not matched:
            return f"没有匹配到「{keyword}」的皮肤类型。"
        equip_type = str(matched[0].get("equip_type"))
        tname = str(matched[0].get("equip_type_name") or keyword)
        # 价格升序扫描前几页后取全局最低，价格升序时最便宜的通常在第 1 页。
        scan_pages = max(1, min(10, int(self.config.get("lowest_price_scan_pages", 5))))
        data = await self._collect_from_pages(
            equip_type, lambda it: True, order_by="price ASC", scan_pages=scan_pages
        )
        items = (data.get("result") or [])
        if not items:
            return f"没有找到 {tname} 的在售商品。"
        best = min(items, key=lambda it: _price_fen(it) or float("inf"))
        return format_lowest_price(best, tname, len(items))

    # ---------- 收藏 ----------
    def _load_favorites(self):
        try:
            if os.path.exists(self._favorite_file):
                with io.open(self._favorite_file, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    return data
        except Exception as exc:
            logger.warning("yjcbg load favorites failed: %s", exc)
        return []

    def _save_favorites(self):
        try:
            with io.open(self._favorite_file, "w", encoding="utf-8") as f:
                json.dump(self._favorites, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.warning("yjcbg save favorites failed: %s", exc)

    async def _handle_fav(self, event, args: list[str]) -> str:
        if not args:
            return "用法：/永劫藏宝阁 收藏 编号\n例如：/永劫藏宝阁 收藏 Y002481"
        id_token = _extract_id_token(" ".join(args).strip())
        if not id_token:
            return "请提供有效的 Y 编号，例如：/永劫藏宝阁 收藏 Y002481"
        user_id = event.get_sender_id() or "default"
        data = await self._search_by_id(id_token, 1, 1)
        items = (data.get("result") or [])
        if not items:
            return f"没找到编号 {id_token} 的在售商品，暂无法收藏。"
        it = items[0]
        ordersn = _ordersn(it) or id_token
        for fav in self._favorites:
            if fav.get("user_id") == user_id and fav.get("ordersn") == ordersn:
                return f"编号 {ordersn} 已在你的收藏中。可用 /永劫藏宝阁 收藏列表 查看。"
        snap = {
            "user_id": user_id,
            "ordersn": ordersn,
            "name": _pick(it, "format_equip_name", "equip_name", "title", "name", "role_name", default=ordersn),
            "variation": _variation(it),
            "server": _pick(it, "server_name", "serverid", "server_id", "server"),
            "price_fen": _price_fen(it),
            "link": _build_link(it),
            "added_at": time.strftime("%Y-%m-%d %H:%M"),
        }
        self._favorites.append(snap)
        self._save_favorites()
        return f"已收藏：{snap['name']}（{ordersn}）。可用 /永劫藏宝阁 收藏列表 查看，/永劫藏宝阁 刷新收藏 更新价格。"

    async def _handle_unfav(self, event, args: list[str]) -> str:
        user_id = event.get_sender_id() or "default"
        if not args:
            removed = [f for f in self._favorites if f.get("user_id") == user_id]
            self._favorites = [f for f in self._favorites if f.get("user_id") != user_id]
            self._save_favorites()
            return f"已取消你的全部 {len(removed)} 条收藏。" if removed else "你当前没有收藏。"
        id_token = _extract_id_token(" ".join(args).strip())
        if not id_token:
            return "请提供要取消的编号：/永劫藏宝阁 取消收藏 编号"
        before = len(self._favorites)
        self._favorites = [
            f for f in self._favorites
            if not (f.get("user_id") == user_id and id_token.lower() in str(f.get("ordersn", "")).lower())
        ]
        self._save_favorites()
        removed = before - len(self._favorites)
        return f"已取消编号 {id_token} 的 {removed} 条收藏。" if removed else f"没有找到编号 {id_token} 的收藏。"

    def _handle_fav_list(self, event) -> str:
        user_id = event.get_sender_id() or "default"
        mine = [f for f in self._favorites if f.get("user_id") == user_id]
        if not mine:
            return "你还没有收藏。可用 /永劫藏宝阁 收藏 编号 添加。"
        lines = [f"你的收藏（{len(mine)}）："]
        for i, f in enumerate(mine, 1):
            price = f"{f.get('price_fen', 0) / 100:.2f} 元" if f.get("price_fen") else "价格未知"
            name = f.get("name") or f.get("ordersn") or ""
            if f.get("variation"):
                name += f"（{f['variation']}）"
            entry = f"{i}. {name}｜{price}｜{f.get('ordersn')}"
            if f.get("server"):
                entry += f"\n   服务器：{f['server']}"
            if f.get("link"):
                entry += f"\n   链接：{f['link']}"
            lines.append(entry)
        lines.append("\n可用 /永劫藏宝阁 刷新收藏 更新全部价格。")
        return "\n\n".join(lines)

    async def _handle_refresh_fav(self, event) -> str:
        user_id = event.get_sender_id() or "default"
        mine = [f for f in self._favorites if f.get("user_id") == user_id]
        if not mine:
            return "你还没有收藏。可用 /永劫藏宝阁 收藏 编号 添加。"
        changed = 0
        dropped = 0
        for fav in mine:
            ordersn = fav.get("ordersn") or ""
            if not ordersn:
                continue
            try:
                data = await self._search_by_id(ordersn, 1, 1)
                items = (data.get("result") or [])
                if not items:
                    dropped += 1
                    continue
                it = items[0]
                new_fen = _price_fen(it)
                new_link = _build_link(it)
                if new_fen is not None and new_fen != fav.get("price_fen"):
                    fav["price_fen"] = new_fen
                    changed += 1
                if new_link:
                    fav["link"] = new_link
            except Exception:
                continue
        self._save_favorites()
        lines = [f"已刷新 {len(mine)} 条收藏。"]
        if changed:
            lines.append(f"价格变化：{changed} 条")
        else:
            lines.append("价格无变化")
        if dropped:
            lines.append(f"⚠ 已下架/查不到：{dropped} 条")
        lines.append("可用 /永劫藏宝阁 收藏列表 查看最新。")
        return "\n".join(lines)

    # ---------- 诊断 ----------
    async def _handle_diag(self, event, args: list[str]) -> str:
        lines = ["永劫无间藏宝阁诊断"]
        lines.append("\n【配置与依赖】")
        lines.append(f"基础地址：{self.config.get('base_url')}")
        lines.append(f"Cookie：{'已填写' if (self.config.get('cookie') or '').strip() else '未填写（仅公共查询，可能搜不到数据）'}")
        lines.append(f"每页数量：{self.config.get('page_size')}")
        lines.append(f"并发/限流间隔：{self.config.get('request_concurrency')}/{self.config.get('request_interval')} 秒")
        lines.append(f"收藏数：{len(self._favorites)}，提醒数：{len(self._reminders)}")
        try:
            import httpx  # noqa: F401
            lines.append("依赖 httpx：可用")
        except ImportError:
            lines.append("依赖 httpx：缺失！请执行 pip install -r requirements.txt")
        lines.append("\n【接口连通与风控状态】")
        try:
            data = await self.client.get_skin_types("", 1, 5)
            types = (data.get("equip_type_list") or [])
            if types:
                lines.append(f"✓ get_aggregate_equip_type_list 正常，返回 {len(types)} 个皮肤类型")
            else:
                lines.append("⚠ 接口返回 0 个皮肤类型：可能 Cookie 失效、需要登录态，或接口已调整")
            lines.append("✓ 未触发风控（CAPTCHA/MOBILE_AUTH）")
        except MobileAuthRequired:
            lines.append("✗ 触发手机短信验证（MOBILE_AUTH）——需在配置里填写有效 Cookie 或完成短信验证")
        except CbgClientError as exc:
            lines.append(f"✗ 接口异常：{exc}")
        lines.append("\n提示：若「接口测试」异常，优先检查 Cookie 是否过期、请求是否过于频繁触发风控。")
        return "\n".join(lines)

    async def _enrich_items(self, items):
        async def enrich(it):
            try:
                sn = it.get("game_ordersn") or ""
                sid = str(it.get("serverid"))
                det = await self.client.detail(sid, sn)
                ed = det.get("equip_desc")
                if isinstance(ed, str):
                    rc = json.loads(ed).get("raw_content", {})
                elif isinstance(ed, dict):
                    rc = ed.get("raw_content", {})
                else:
                    rc = {}
                it["_seller"] = rc.get("nick_name") or ""
                it["_washed_count"] = rc.get("washed_count", "")
            except Exception as e:
                logger.warning("yjcbg enrich failed: %s", e)
                it["_seller"] = ""
                it["_washed_count"] = ""
        await asyncio.gather(*[enrich(it) for it in items])

    def _format_error(self, exc: Exception) -> str:
        if bool(self.config.get("debug_raw_error", False)):
            return f"查询失败：{exc}"
        if isinstance(exc, CbgClientError):
            return f"查询失败：{exc}"
        return (
            "查询失败。藏宝阁接口可能需要登录态 Cookie、触发了风控，或接口路径已变化。"
            "可在插件配置里填写 Cookie 后重试。"
        )

    async def terminate(self):
        if self._reminder_task is not None:
            self._reminder_task.cancel()
            try:
                await self._reminder_task
            except (asyncio.CancelledError, Exception):
                pass
        await self.client.close()


def _split_args(message: str) -> list[str]:
    parts = message.strip().split()
    if parts and parts[0].lstrip("/!！").lower() in {"yjcbg", "永劫藏宝阁", "永劫cbg", "藏宝阁"}:
        return parts[1:]
    return parts

def _map_sort_order(sort_order: str | None) -> str:
    """Map plugin sort_order (price_asc etc.) to API order_by param.

    Default (None / no sort specified) is price ASC.
    """
    mapping = {
        "price_asc": "price ASC",
        "price_desc": "price DESC",
        "collect_asc": "collect_num ASC",
        "collect_desc": "collect_num DESC",
    }
    return mapping.get(sort_order, "price ASC")


def _item_matches_id(item: dict, keyword: str) -> bool:
    """Return True if the item's basic_attrs (编号) contains keyword (fuzzy)."""
    attrs = (item.get("other_info") or {}).get("basic_attrs") or []
    combined = " ".join(str(a) for a in attrs).lower()
    return keyword in combined


def _matches_star_ranges(item: dict, ranges) -> bool:
    """Return True if the item's variation_id 星格 falls within the given ranges.

    star_ranges is a list of (lo, hi) tuples; each corresponds to one
    星格 segment in variation_id (separated by "-").
    """
    if not ranges:
        return True
    var = (item.get("variation_info") or {}).get("variation_id") or ""
    other = item.get("other_info")
    if not var and isinstance(other, dict):
        other_var = (other.get("variation_info") or {}).get("variation_id")
        var = str(other_var) if other_var else ""
    segs = [s for s in str(var).split("-") if s != ""]
    for i, (lo, hi) in enumerate(ranges):
        if lo is None and hi is None:
            continue
        if i >= len(segs):
            return False
        try:
            v = int(segs[i])
        except ValueError:
            return False
        if lo is not None and v < lo:
            return False
        if hi is not None and v > hi:
            return False
    return True


def _extract_id_token(keyword: str) -> str:
    """Extract a letter+number token from a combined keyword.

    Supports Y/S 等任意单个字母开头的编号，如 "风之絮语 Y002481" -> "Y002481",
    "S001234" -> "S001234". Returns "" if no such token is found.
    """
    for token in str(keyword).split():
        if re.match(r'^[a-zA-Z]\d+$', token):
            return token
    return ""
