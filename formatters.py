from __future__ import annotations

import json
import re
from typing import Any


def format_help() -> str:
    return (
        "永劫无间藏宝阁查询\n"
        "命令入口：/永劫藏宝阁（别名：/藏宝阁 /永劫cbg /yjcbg）\n"
        "\n"
        "基础查询：\n"
        "/永劫藏宝阁 帮助  - 显示本帮助\n"
        "/永劫藏宝阁 搜索 关键词  - 搜索商品/账号（默认价格升序）\n"
        "/永劫藏宝阁 搜索 皮肤名 编号  - 皮肤名+编号组合搜索\n"
        "/永劫藏宝阁 搜索 编号  - 编号模糊搜索（如 Y002481）\n"
        "/永劫藏宝阁 搜索 关键词 页码  - 翻页（末尾加数字，如 搜索 胡桃 2）\n"
        "\n"
        "搜索选项（可追加）：\n"
        "  价格升 / 价格降 / 收藏升 / 收藏降\n"
        "  公示期 / 在售期\n"
        "  1星格:2000-3000 2星格:100-200（未指定星格不限制）\n"
        "  价格:100-500（价格区间，元；可写 价格:100- 或 价格:-500）\n"
        "  以上过滤与排序可同时组合使用\n"
        "\n"
        "最低价查询：\n"
        "/永劫藏宝阁 最低价 关键词  - 返回该皮肤当前最便宜的一件\n"
        "\n"
        "收藏（按用户区分，快照可刷新）：\n"
        "/永劫藏宝阁 收藏 编号  - 收藏某个编号的商品\n"
        "/永劫藏宝阁 收藏列表  - 查看我的收藏\n"
        "/永劫藏宝阁 刷新收藏  - 更新我的收藏的最新价格\n"
        "/永劫藏宝阁 取消收藏 编号  - 取消对应收藏\n"
        "\n"
        "提醒（按用户区分，触发时会艾特添加者）：\n"
        "/永劫藏宝阁 提醒 皮肤名 编号  - 编号出现时提醒我\n"
        "/永劫藏宝阁 提醒列表  - 查看我的提醒\n"
        "/永劫藏宝阁 取消提醒 编号  - 取消对应提醒\n"
        "/永劫藏宝阁 取消提醒  - 取消我的全部提醒\n"
        "\n"
        "诊断：\n"
        "/永劫藏宝阁 诊断  - 检查 Cookie 有效性、接口连通与风控状态、配置\n"
        "\n"
        "风控验证：\n"
        "/永劫藏宝阁 验证码 123456  - 输入手机短信验证码完成验证\n"
        "\n"
        "示例：\n"
        "/永劫藏宝阁 搜索 胡桃 价格降\n"
        "/永劫藏宝阁 搜索 胡桃 1星格:2000-3000 2星格:100-200\n"
        "/永劫藏宝阁 搜索 胡桃 公示期 价格:100-500 价格升\n"
        "/永劫藏宝阁 最低价 风之絮语\n"
        "/永劫藏宝阁 收藏 Y002481\n"
        "/永劫藏宝阁 搜索 风之絮语 Y002481"
    )



def _matches_query(item, query):
    q = str(query or "").strip().lower()
    if not q:
        return True
    other = item.get("other_info") or {}
    texts = [
        _pick(item, "format_equip_name", "equip_name", "title", "name", "role_name"),
        _extra_desc(item),
        _variation(item),
        _ordersn(item),
        _pick(item, "serverid", "server_id"),
        other.get("extra_desc_sumup_short", ""),
        other.get("desc_sumup_short", ""),
        other.get("desc_sumup", ""),
        item.get("game_ordersn", ""),
        item.get("eid", ""),
    ]
    # Build combined text for fuzzy match
    combined = " ".join([str(t) for t in texts if t]).lower()
    if q in combined:
        return True
    # Partial word match
    for part in q.split():
        if part in combined:
            return True
    return False


def parse_star_ranges(spec):
    if not spec:
        return []
    ranges = []
    for part in str(spec).replace("，", ",").split(","):
        part = part.strip()
        if not part or part == "-":
            ranges.append((None, None))
            continue
        lo = hi = None
        if "-" in part:
            a, _, b = part.partition("-")
            a = a.strip(); b = b.strip()
            lo = int(a) if a.isdigit() else None
            hi = int(b) if b.isdigit() else None
        else:
            if part.isdigit():
                lo = hi = int(part)
        ranges.append((lo, hi))
    return ranges


def parse_star_position_ranges(keyword: str):
    """Parse 'N星格:range' tokens from a keyword.

    e.g. "胡桃 1星格:2000-3000 2星格:100-200" ->
          ranges[(2000,3000),(100,200),...], remaining "胡桃".

    Positions not specified are left as (None, None) (not restricted).
    Returns (ranges, remaining_keyword).
    """
    import re
    pattern = re.compile(r"(\d+)\s*星格\s*[:：]\s*([^\s]+)")
    matches = pattern.findall(keyword)
    remaining = pattern.sub("", keyword).strip()
    ranges: list[tuple[int | None, int | None]] = []
    for pos, spec in matches:
        pos = int(pos)
        lo = hi = None
        if "-" in spec:
            a, _, b = spec.partition("-")
            a = a.strip(); b = b.strip()
            lo = int(a) if a.isdigit() else None
            hi = int(b) if b.isdigit() else None
        elif spec.isdigit():
            lo = hi = int(spec)
        while len(ranges) < pos:
            ranges.append((None, None))
        ranges[pos - 1] = (lo, hi)
    return ranges, remaining


def parse_price_range(keyword: str):
    """Parse a '价格:lo-hi' token from a keyword.

    lo/hi are in 元. Either bound may be omitted（价格:100-、价格:-500），
    or a single value given（价格:500，视为精确匹配）。
    Returns (price_range, remaining_keyword) where price_range=(lo,hi) or None.
    """
    m = re.search(r"价格\s*[:：]\s*(-?[0-9.]+(?:\s*-\s*[0-9.]+)?)", keyword)
    if not m:
        return None, keyword
    spec = m.group(1).strip()
    remaining = re.sub(r"价格\s*[:：]\s*-?[0-9.]+(?:\s*-\s*[0-9.]+)?", "", keyword).strip()
    if "-" in spec:
        a, _, b = spec.partition("-")
        lo = int(float(a)) if a.strip() else None
        hi = int(float(b)) if b.strip() else None
    else:
        lo = hi = int(float(spec))
    return (lo, hi), remaining


def _star_ranges_desc(ranges):
    if not ranges:
        return ""
    parts = []
    for i, (lo, hi) in enumerate(ranges):
        if lo is None and hi is None:
            continue
        pos = i + 1
        if lo is not None and hi is not None and lo == hi:
            parts.append(f"{pos}星格:{lo}")
        else:
            parts.append(f"{pos}星格:{lo or ''}-{hi or ''}")
    if not parts:
        return ""
    return "星格：" + " ".join(parts)


def _matches_star_range(item, ranges):
    if not ranges:
        return True
    var = (item.get("variation_info") or {}).get("variation_id") or ""
    other = item.get("other_info")
    if not var and isinstance(other, dict):
        oi_var = (other.get("variation_info") or {}).get("variation_id")
        var = str(oi_var) if oi_var else ""
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


def _price_fen(item: dict[str, Any]) -> int | None:
    """Extract the item price as integer 分（fen）. Returns None if unavailable."""
    for key in ("price", "selling_price", "equip_price", "format_price"):
        v = item.get(key)
        if v in (None, ""):
            continue
        if isinstance(v, (int, float)):
            return int(v)
        try:
            text = str(v).replace("￥", "").replace("元", "").replace(",", "").strip()
            if not text:
                continue
            if "." in text:
                return int(round(float(text) * 100))
            return int(float(text))
        except (ValueError, TypeError):
            continue
    return None


def _matches_price_range(item: dict[str, Any], price_range) -> bool:
    if not price_range:
        return True
    lo, hi = price_range
    fen = _price_fen(item)
    if fen is None:
        return True  # 解析不到价格时不拦截
    if lo is not None and fen < lo * 100:
        return False
    if hi is not None and fen > hi * 100:
        return False
    return True


def _price_range_desc(price_range) -> str:
    if not price_range:
        return ""
    lo, hi = price_range
    if lo is not None and hi is not None and lo == hi:
        return f"价格:{lo}元"
    if lo is not None and hi is not None:
        return f"价格:{lo}-{hi}元"
    if lo is not None:
        return f"价格:≥{lo}元"
    return f"价格:≤{hi}元"


def format_search_result(data: dict[str, Any], max_results: int, page: int = 1, query: str = "", star_ranges=None, fair_show_filter=None, sort_order=None, skip_query_filter: bool = False, price_range=None) -> str:
    items = _extract_items(data)
    if not items:
        return "没有解析到搜索结果。可能是关键词无结果、接口需要登录态，或藏宝阁接口已调整。"

    items = _rank_items(items, query)
    if query.strip() and not skip_query_filter:
        items = [item for item in items if _matches_query(item, query)]

    if star_ranges:
        items = [item for item in items if _matches_star_range(item, star_ranges)]
    if price_range:
        items = [item for item in items if _matches_price_range(item, price_range)]
    # Y-number exact filter: when query looks like a Y-number, only show items whose basic_attrs number matches
    if re.match(r"^y\d+$", query.strip().lower()):
        items = [item for item in items if query.strip().lower() in _ordersn(item).strip().lower()]
    if fair_show_filter is not None:
        items = [item for item in items if item.get("pass_fair_show") == fair_show_filter]
    if sort_order == "price_asc":
        items.sort(key=lambda it: float(it.get("price", 0) or 0))
    elif sort_order == "price_desc":
        items.sort(key=lambda it: float(it.get("price", 0) or 0), reverse=True)
    elif sort_order == "collect_asc":
        items.sort(key=lambda it: int(it.get("collect_num", 0) or 0))
    elif sort_order == "collect_desc":
        items.sort(key=lambda it: int(it.get("collect_num", 0) or 0), reverse=True)
    else:
        items.sort(key=lambda it: float(it.get("price", 0) or 0))
    desc_parts = []
    if star_ranges:
        desc_parts.append(_star_ranges_desc(star_ranges))
    if price_range:
        desc_parts.append(_price_range_desc(price_range))
    if fair_show_filter is not None:
        label = "公示期" if fair_show_filter == 0 else "在售期"
        desc_parts.append(label)
    sort_names = {"price_asc": "价格升序", "price_desc": "价格降序", "collect_asc": "收藏升序", "collect_desc": "收藏降序"}
    if sort_order in sort_names:
        desc_parts.append(sort_names[sort_order])
    desc = "  ".join(desc_parts)
    total_filtered = len(items)
    # 优先使用 API 返回的 pager 分页信息（recommend.py 等接口提供 total_pages）
    pager = data.get("pager") or {}
    api_total_pages = pager.get("total_pages")
    if isinstance(api_total_pages, int) and api_total_pages > 0:
        total_pages = api_total_pages
    else:
        total_pages = max(1, -(-total_filtered // max_results))
    page = max(1, min(page, total_pages))
    start_idx = 0
    end_idx = start_idx + max_results
    page_items = items[start_idx:end_idx]
    header = "永劫无间藏宝阁搜索结果："
    if desc:
        header += "  " + desc
    header += f"  ({page}/{total_pages}页, 共{total_filtered}条)"
    lines = [header]
    for index, item in enumerate(page_items, start=start_idx + 1):
        lines.append(_format_item(index, item))
    if total_pages > 1:
        if page < total_pages:
            lines.append(f"下一页: 搜索 {query} {page + 1}")
        else:
            lines.append("已是最后一页")
    return "\n\n".join(lines)


def format_lowest_price(item: dict[str, Any], tname: str, scanned: int) -> str:
    body = _format_item(1, item)
    return f"💰 {tname} 当前最低价（扫描 {scanned} 条在售）：\n\n" + body


def format_detail_result(data: dict[str, Any]) -> str:
    item = _extract_detail(data)
    if not item:
        return "没有解析到商品详情。请确认 serverid 和 ordersn 是否正确，或在配置里填写 Cookie 后重试。"
    return "永劫无间藏宝阁商品详情：\n" + _format_item(1, item, detail=True)


def format_raw_result(data: dict[str, Any]) -> str:
    text = json.dumps(data, ensure_ascii=False, indent=2)
    if len(text) > 1800:
        text = text[:1800] + "\n... 已截断"
    return text




def _variation(equip):
    variation_info = equip.get("variation_info")
    other = equip.get("other_info")
    if not isinstance(variation_info, dict):
        variation_info = other.get("variation_info") if isinstance(other, dict) else None
    if not isinstance(variation_info, dict):
        return ""
    value = variation_info.get("variation_name")
    return str(value).strip() if value else ""





def _star_grid(equip):
    """Extract star grid (星格) values from variation_id (hyphen-separated segments).
    e.g. variation_id='1234-5678-90' -> '1234-5678-90'"""
    var = (equip.get("variation_info") or {}).get("variation_id") or ""
    other = equip.get("other_info")
    if not var and isinstance(other, dict):
        oi_var = (other.get("variation_info") or {}).get("variation_id")
        var = str(oi_var) if oi_var else ""
    var = str(var).strip()
    if not var:
        return ""
    # Normalize: keep only numeric segments separated by '-'
    segs = [s for s in var.split("-") if s.strip() != ""]
    if not segs:
        return ""
    return "-".join(segs)


def _extra_desc(equip):
    value = equip.get("extra_desc_sumup_short")
    other = equip.get("other_info")
    if value in (None, "") and isinstance(other, dict):
        value = other.get("extra_desc_sumup_short")
    return str(value).strip() if value else ""


def _format_item(index: int, item: dict[str, Any], detail: bool = False) -> str:
    title = _pick(
        item,
        "format_equip_name",
        "equip_name",
        "title",
        "name",
        "role_name",
        "desc_sumup_short",
        "desc",
        default="永劫无间商品",
    )
    price = _format_price(_pick(item, "price", "selling_price", "equip_price", "format_price"))
    server = _pick(item, "server_name", "serverid", "server_id", "server")
    ordersn = _ordersn(item)
    status = _pick(item, "status_desc", "status", "selling_status")
    listed_at = _pick(item, "create_time", "show_time", "selling_time", "update_time")
    fair_show_end = _pick(item, "fair_show_end_time", "fair_end_time", "public_end_time")
    selling_start = _pick(item, "selling_time", "sale_start_time", "onsale_time")
    selling_end = _pick(item, "expire_time", "selling_end_time", "sale_end_time")
    draw_start = _pick(item, "random_draw_start_time", "draw_start_time", "lottery_start_time")
    draw_end = _pick(item, "random_draw_end_time", "draw_end_time", "lottery_end_time")
    is_random_draw = item.get("is_random_draw_period")
    area = _pick(item, "area_name", "platform_desc", "game_channel")
    summary = _pick(item, "desc_sumup_short", "subtitle", "level_desc")

    variation = _variation(item)
    extra = _extra_desc(item)

    lines = [f"{index}. {title}"]
    if variation and variation != title:
        lines.append(f"变体：{variation}")
    if extra and extra != title and extra != variation:
        lines.append(f"品质/英雄：{extra}")

    if price:
        lines.append(f"价格：{price}")
    if server:
        lines.append(f"服务器：{server}")
    if area:
        lines.append(f"区服/平台：{area}")
    if ordersn:
        lines.append(f"编号：{ordersn}")
    star_grid = _star_grid(item)
    if star_grid:
        lines.append(f"星格：{star_grid}")
    collect_num = _pick(item, "collect_num")
    if collect_num not in (None, "", 0):
        lines.append(f"收藏数：{collect_num}")
    seller_name = item.get("_seller") or ""
    if seller_name.strip():
        lines.append(f"卖家：{seller_name}")
    washed = item.get("_washed_count")
    if washed not in (None, "", 0):
        lines.append(f"洗练次数：{washed}")
    if status:
        lines.append(f"状态：{_format_status(status)}")
    if fair_show_end:
        lines.append(f"公示期：截至 {fair_show_end}")
    if selling_start and selling_end:
        lines.append(f"在售期：{selling_start} 至 {selling_end}")
    elif selling_start:
        lines.append(f"在售期：{selling_start} 起")
    elif listed_at:
        lines.append(f"在售期：{listed_at} 起")
    if isinstance(is_random_draw, bool):
        draw_text = "抽签中" if is_random_draw else "不在抽签期"
        if is_random_draw and (draw_start or draw_end):
            draw_text += f"（{draw_start or '未知开始时间'} 至 {draw_end or '未知结束时间'}）"
        lines.append(f"抽签状态：{draw_text}")
    if summary and summary != title:
        lines.append(f"摘要：{summary}")

    link = _build_link(item)
    if link:
        lines.append(f"链接：{link}")

    if detail:
        friendly_detail = _friendly_detail_summary(item)
        if friendly_detail:
            lines.append(friendly_detail)
    return "\n".join(lines)


def _extract_items(data: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("equip_type_list", "result", "equip_list", "data", "list", "items"):
        candidate = data.get(key)
        if isinstance(candidate, list):
            return [it for it in candidate if isinstance(it, dict)]
    return []
def _rank_items(items: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    query = query.strip().lower()
    if not query:
        return items

    def _text(item: dict[str, Any]) -> list[str]:
        values = [
            _ordersn(item),
            _pick(item, "serverid", "server_id"),
            _pick(item, "format_equip_name", "equip_name", "title", "name", "role_name"),
            _extra_desc(item),
            _variation(item),
        ]
        return [str(value).lower() for value in values if value]

    def score(item: dict[str, Any]) -> int:
        identifiers = _text(item)
        normalized = [str(value).lower() for value in identifiers if value]
        if any(value == query for value in normalized):
            return 0
        if any(query in value for value in normalized):
            return 1
        return 2

    return sorted(items, key=score)


def _extract_detail(data: dict[str, Any]) -> dict[str, Any] | None:
    raw_content = data.get("raw_content")
    if isinstance(raw_content, dict):
        merged = dict(raw_content)
        for key in ("display_content", "version"):
            if key in data:
                merged[key] = data[key]
        return merged

    for key in ("equip", "item", "data", "result", "desc", "equip_desc"):
        value = data.get(key)
        if isinstance(value, dict):
            nested = _extract_detail(value)
            return nested or value
        if isinstance(value, str) and value.strip().startswith(("{", "[")):
            try:
                parsed = json.loads(value)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                nested = _extract_detail(parsed)
                return nested or parsed
    return data if any(key in data for key in ("ordersn", "serverid", "price", "equip_name")) else None


def _pick(item: dict[str, Any], *keys: str, default: str = "") -> Any:
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return value
    return default


def _format_price(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        return f"{value / 100:.2f} 元"
    text = str(value)
    return text if any(unit in text for unit in ("元", "￥", ".")) else f"{text} 元"



def _ordersn(item: dict[str, Any]) -> str:
    numbered = _numbered_attr(item)
    if numbered:
        return numbered
    return _pick(item, "ordersn", "order_sn", "game_ordersn", "equipid", "eid")


def _numbered_attr(item: dict[str, Any]) -> str:
    attrs = item.get("basic_attrs")
    if not isinstance(attrs, list):
        other = item.get("other_info")
        attrs = other.get("basic_attrs") if isinstance(other, dict) else None
    if isinstance(attrs, list):
        for line in attrs:
            text = str(line).strip()
            if text.startswith(("编号", "編號")):
                for sep in (":", "："):
                    if sep in text:
                        value = text.split(sep, 1)[1].strip()
                        if value:
                            return value
    return ""


def _format_status(value: Any) -> str:
    status_map = {
        1: "公示中",
        2: "在售中",
        3: "已下架",
        4: "已售出",
    }
    if isinstance(value, int):
        return status_map.get(value, str(value))
    text = str(value)
    return status_map.get(int(text), text) if text.isdigit() else text


def _build_link(item: dict[str, Any]) -> str:
    url = _pick(item, "url", "link", "share_url")
    if url:
        return str(url)
    serverid = _pick(item, "serverid", "server_id")
    ordersn = _pick(item, "ordersn", "order_sn", "game_ordersn")
    if serverid and ordersn:
        return f"https://yjwujian.cbg.163.com/cgi/mweb/equip/{serverid}/{ordersn}"
    return ""


def _friendly_detail_summary(item: dict[str, Any]) -> str:
    screenshots = item.get("capture_url")
    if not screenshots and isinstance(item.get("other_info"), dict):
        screenshots = item["other_info"].get("capture_url")

    notes: list[str] = []
    if isinstance(screenshots, list) and screenshots:
        notes.append(f"商品截图：{len(screenshots)} 张")

    collection_score = item.get("collection_score")
    if isinstance(collection_score, (int, float)) and collection_score > 0:
        notes.append(f"收藏评分：{collection_score}")

    if notes:
        return "详情：" + "；".join(notes)
    return "详情：该商品的公开详情较少，请打开上方链接查看完整外观、英雄、武器和账号资产。"
