def plural(n: int, one: str, few: str, many: str) -> str:
    """«1 позиция», «3 позиции», «5 позиций»."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} {one}"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} {few}"
    return f"{n} {many}"


def rub(v: float) -> str:
    return f"{v:,.2f}".replace(",", " ").replace(".", ",") + " ₽"
