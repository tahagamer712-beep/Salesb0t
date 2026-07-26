"""Fuzzy matching for product and pharmacy names."""
from difflib import SequenceMatcher


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def fuzzy_match(name: str, known: list, cutoff: float = 0.70) -> str | None:
    """Return the closest known name if similarity >= cutoff, else None."""
    if not known or not name:
        return None
    name_l = name.lower()
    best, best_score = None, 0.0
    for k in known:
        score = _similarity(name_l, k.lower())
        if score > best_score:
            best, best_score = k, score
    return best if best_score >= cutoff else None


def fuzzy_match_product(name: str, known: list) -> str | None:
    return fuzzy_match(name, known, cutoff=0.72)


def fuzzy_match_pharmacy(name: str, known: list) -> str | None:
    return fuzzy_match(name, known, cutoff=0.65)


def fuzzy_match_candidates(name: str, known: list, cutoff: float = 0.65, top_n: int = 5) -> list:
    """يرجع كل الأسماء المرشّحة (مو بس الأفضل) فوق cutoff، مرتبة تنازلياً حسب التشابه.
    كل عنصر: (الاسم, درجة التشابه). يفيد لاكتشاف حالة الغموض (صيدليتين متشابهات)
    بدل ما نختار وحدة بصمت زي fuzzy_match_pharmacy العادية."""
    if not known or not name:
        return []
    name_l = name.lower()
    scored = [(k, _similarity(name_l, k.lower())) for k in known]
    scored = [s for s in scored if s[1] >= cutoff]
    scored.sort(key=lambda s: s[1], reverse=True)
    return scored[:top_n]


def detect_contradictions(data: dict) -> list[str]:
    """Return list of warning strings for illogical order data."""
    from datetime import date
    warnings = []
    try:
        if data.get("order_date"):
            od = date.fromisoformat(data["order_date"])
            if od > date.today():
                warnings.append(f"⚠️ التاريخ {od} في المستقبل!")
            elif (date.today() - od).days > 90:
                warnings.append(f"⚠️ التاريخ {od} قديم جداً (أكثر من 90 يوم).")
    except Exception:
        pass
    qty = data.get("quantity") or 0
    if qty and int(qty) > 500:
        warnings.append(f"⚠️ الكمية {qty} كبيرة جداً — تأكد من الرقم.")
    price = data.get("price") or 0
    if price and float(price) > 1_000_000:
        warnings.append(f"⚠️ السعر {price} غير منطقي.")
    return warnings


