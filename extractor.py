"""Извлечение условий договоров поставки из TXT-документов.

Подход — правила и регулярные выражения, без LLM:
каждое значение привязано к конкретному абзацу исходного файла,
поэтому его всегда можно показать цитатой и проверить.

Правило актуальности (из задания): условия меняет только допсоглашение,
подписанное обеими сторонами и вступившее в силу к дате проверки.
Проекты, письма и неподписанные/не вступившие ДС показываются отдельно
как несогласованные изменения. Имена файлов и порядок загрузки
на приоритет не влияют.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime

# ---------------------------------------------------------------- регулярки

DATE = r"(\d{1,2}\.\d{1,2}\.\d{4})"
# 1 380 000 | 1380000 | 1 380 000,50 (пробел, неразрывный или узкий пробел)
NUM = r"(\d{1,3}(?:[   ]\d{3})+|\d+)(?:,(\d{1,2}))?"
CURRENCY = r"(?:\s*\([^)]*\))?\s*(?:руб|₽|р\.)"

AMOUNT_PATTERNS = [
    # «Общая стоимость поставки составляет 1 200 000 (…) рублей»
    # «изменить общую стоимость поставки на 1 450 000 рублей»
    re.compile(
        r"общ\w*\s+(?:итогов\w*\s+)?стоимост\w*(?:\s+[а-яё]+){0,2}?\s*"
        r"(?:составляет|составит|равна|на|[:—-])\s*" + NUM + CURRENCY,
        re.I,
    ),
    re.compile(
        r"цен\w*\s+договора(?:\s+[а-яё]+){0,2}?\s*"
        r"(?:составляет|составит|на|[:—-])\s*" + NUM + CURRENCY,
        re.I,
    ),
]

DELIVERY_PATTERNS = [
    # «Поставщик обязуется поставить оборудование не позднее 15.04.2026»
    re.compile(r"постав\w*(?:\s+[а-яё]+){0,4}?\s+не\s+позднее\s+" + DATE, re.I),
    # «изменить срок поставки на 10.05.2026», «Срок поставки: до 10.05.2026»
    re.compile(r"срок\w*\s+поставки\s*(?:на|до|[:—-])\s*(?:до\s+)?" + DATE, re.I),
]
# «перенести срок поставки с 20.03.2026 на 27.03.2026»
DELIVERY_SHIFT = re.compile(r"срок\w*\s+поставки\s+с\s+" + DATE + r"\s+на\s+" + DATE, re.I)

SUPPLIER = re.compile(r"Поставщик\s*[:—-]\s*(.+?)\s*(?:\.(?=\s|$)|;|$)")
LETTER_FROM = re.compile(r"^От\s*:\s*(.+?)\s*$", re.I)
STATUS = re.compile(r"^Статус\s*:\s*(.+)$", re.I)
DOC_DATE = re.compile(r"^Дата(?:\s+проекта)?\s*:\s*" + DATE, re.I)
EFFECTIVE = re.compile(r"вступает\s+в\s+силу\s+(?:с\s+)?" + DATE, re.I)
TITLE_NUMBER = re.compile(r"№\s*([A-Za-zА-Яа-яЁё0-9][\w/-]*)")
CONTRACT_REF = re.compile(r"\b([A-ZА-ЯЁ]{2,}-\d+(?:/\d+)?)\b")
MENTIONS_PRICE = re.compile(r"стоимост|цена|цену|цены", re.I)
MENTIONS_DELIVERY = re.compile(r"срок\w*\s+поставки|поставить|поставки\s+не\s+позднее", re.I)
CHANGE_WORDS = re.compile(r"изложить|изменить|перенести|заменить|продлить", re.I)

# ---------------------------------------------------------------- модель


@dataclass
class Paragraph:
    doc: str
    index: int  # порядковый номер абзаца в документе (с 1)
    line: int  # номер строки в файле (с 1)
    text: str

    @property
    def anchor(self) -> str:
        return make_anchor(self.doc, self.index)


@dataclass
class Evidence:
    paragraph: Paragraph
    match: str  # совпавший фрагмент — для подсветки

    @property
    def doc(self) -> str:
        return self.paragraph.doc

    @property
    def quote(self) -> str:
        return self.paragraph.text


@dataclass
class Document:
    name: str
    paragraphs: list[Paragraph]
    kind: str  # contract | amendment | draft | letter | unknown
    title: str
    number: str | None = None  # собственный номер (договора или ДС)
    doc_date: date | None = None
    status: str | None = None
    signed_by_both: bool | None = None  # None — статус не указан
    effective: date | None = None
    effective_ev: Evidence | None = None
    supplier: str | None = None
    contract_refs: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        names = {
            "contract": "Договор",
            "amendment": "Доп. соглашение",
            "draft": "Проект изменения",
            "letter": "Письмо",
            "unknown": "Документ",
        }
        base = names[self.kind]
        if self.kind == "amendment" and self.number:
            base += f" № {self.number}"
        if self.doc_date:
            base += f" от {fmt_date(self.doc_date)}"
        return base


@dataclass
class FieldValue:
    value: object | None = None  # int (копейки) для суммы, date для срока
    evidence: Evidence | None = None
    source: Document | None = None  # каким документом установлено
    changed: bool = False  # изменено допсоглашением
    history: list[tuple[str, object, Evidence]] = field(default_factory=list)
    note: str | None = None  # пояснение, почему нет данных
    note_ev: Evidence | None = None


@dataclass
class PendingChange:
    field: str  # "amount" | "delivery"
    value: object
    reason: str  # почему не применено
    doc: Document
    evidence: Evidence


@dataclass
class ContractResult:
    number: str
    contract: Document
    supplier: str | None
    supplier_ev: Evidence | None
    amount: FieldValue
    delivery: FieldValue
    related: list[Document] = field(default_factory=list)
    pending: list[PendingChange] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)

    def pending_for(self, field_name: str) -> list[PendingChange]:
        return [p for p in self.pending if p.field == field_name]


@dataclass
class Analysis:
    check_date: date
    documents: list[Document]
    contracts: list[ContractResult]
    warnings: list[str]

    def get(self, number: str) -> ContractResult | None:
        number = number.strip().upper()
        for c in self.contracts:
            if c.number.upper() == number:
                return c
        return None


# ---------------------------------------------------------------- утилиты


def make_anchor(doc: str, index: int) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]", lambda m: f"_{ord(m.group()):x}", doc)
    return f"src-{safe}-p{index}"


def parse_date(s: str) -> date | None:
    try:
        return datetime.strptime(s, "%d.%m.%Y").date()
    except ValueError:
        return None


def fmt_date(d: date | None) -> str:
    return d.strftime("%d.%m.%Y") if d else "нет данных"


def parse_amount(whole: str, frac: str | None) -> int:
    rub = int(re.sub(r"\D", "", whole))
    kop = int((frac or "0").ljust(2, "0"))
    return rub * 100 + kop


def fmt_amount(kop: int | None) -> str:
    if kop is None:
        return "нет данных"
    rub, k = divmod(kop, 100)
    s = f"{rub:,}".replace(",", " ")
    return f"{s},{k:02d} ₽" if k else f"{s} ₽"


def fmt_value(field_name: str, v) -> str:
    return fmt_amount(v) if field_name == "amount" else fmt_date(v)


def decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------- разбор документа


def split_paragraphs(name: str, text: str) -> list[Paragraph]:
    paras = []
    for i, line in enumerate(text.splitlines(), start=1):
        if line.strip():
            paras.append(Paragraph(name, len(paras) + 1, i, line.strip()))
    return paras


def classify(title: str, status: str | None, paras: list[Paragraph]) -> str:
    t = title.upper()
    st = (status or "").lower()
    if t.startswith("ПРОЕКТ") or "проект" in st:
        return "draft"
    if "СОГЛАШЕНИЕ" in t:
        return "amendment"
    if t.startswith("ДОГОВОР") or t.startswith("КОНТРАКТ"):
        return "contract"
    if "ПИСЬМО" in t or any(LETTER_FROM.match(p.text) for p in paras[:6]):
        return "letter"
    return "unknown"


def signed_by_both(status: str | None) -> bool | None:
    if status is None:
        return None
    s = status.lower()
    if "не подписан" in s or "проект" in s:
        return False
    if "подписан" in s and "обеими сторонами" in s:
        return True
    return False  # подписано одной стороной / иное


def parse_document(name: str, text: str) -> Document:
    paras = split_paragraphs(name, text)
    title = paras[0].text if paras else ""
    status = None
    doc_date = None
    for p in paras[:8]:
        if status is None and (m := STATUS.match(p.text)):
            status = m.group(1).strip()
        if doc_date is None and (m := DOC_DATE.match(p.text)):
            doc_date = parse_date(m.group(1))
    kind = classify(title, status, paras)
    doc = Document(
        name=name,
        paragraphs=paras,
        kind=kind,
        title=title,
        doc_date=doc_date,
        status=status,
        signed_by_both=signed_by_both(status),
    )
    if m := TITLE_NUMBER.search(title):
        doc.number = m.group(1)
    for p in paras:
        if doc.effective is None and (m := EFFECTIVE.search(p.text)):
            doc.effective = parse_date(m.group(1))
            doc.effective_ev = Evidence(p, m.group(0))
        if doc.supplier is None and (m := SUPPLIER.search(p.text)):
            doc.supplier = m.group(1)
        if kind == "letter" and doc.supplier is None and (m := LETTER_FROM.match(p.text)):
            doc.supplier = m.group(1)
    # ссылки на договоры (для ДС, проектов, писем)
    refs = []
    for p in paras:
        for r in CONTRACT_REF.findall(p.text):
            if r not in refs:
                refs.append(r)
    doc.contract_refs = refs
    return doc


def find_amounts(paras: list[Paragraph]) -> list[tuple[int, Evidence]]:
    out = []
    for p in paras:
        for pat in AMOUNT_PATTERNS:
            for m in pat.finditer(p.text):
                out.append((parse_amount(m.group(1), m.group(2)), Evidence(p, m.group(0))))
    return out


def find_deliveries(paras: list[Paragraph]) -> list[tuple[date, Evidence]]:
    out = []
    for p in paras:
        if DELIVERY_SHIFT.search(p.text):
            continue  # «с … на …» обрабатывается отдельно
        for pat in DELIVERY_PATTERNS:
            for m in pat.finditer(p.text):
                d = parse_date(m.group(1))
                if d and not any(e.paragraph is p and d == v for v, e in out):
                    out.append((d, Evidence(p, m.group(0))))
    for p in paras:
        for m in DELIVERY_SHIFT.finditer(p.text):
            d = parse_date(m.group(2))
            if d:
                out.append((d, Evidence(p, m.group(0))))
    return out


def first_mention(paras: list[Paragraph], pattern: re.Pattern) -> Evidence | None:
    for p in paras:
        if m := pattern.search(p.text):
            return Evidence(p, m.group(0))
    return None


# ---------------------------------------------------------------- анализ комплекта


def _single(found: list, field_name: str, doc: Document, flags: list[str]):
    """Одно значение из документа; при расхождении — флаг проверки."""
    if not found:
        return None, None
    values = {v for v, _ in found}
    if len(values) > 1:
        shown = ", ".join(fmt_value(field_name, v) for v in values)
        what = "суммы" if field_name == "amount" else "срока поставки"
        flags.append(f"{doc.name}: найдено несколько разных значений {what} ({shown}) — требуется проверка.")
        return None, None
    return found[0]


def analyze(files: dict[str, str], check_date: date) -> Analysis:
    """files: имя файла -> текст."""
    docs = [parse_document(n, t) for n, t in files.items()]
    warnings: list[str] = []

    contracts_docs: dict[str, list[Document]] = {}
    for d in docs:
        if d.kind == "contract":
            key = (d.number or d.name).upper()
            contracts_docs.setdefault(key, []).append(d)
        elif d.kind == "unknown":
            warnings.append(f"{d.name}: тип документа не распознан, файл не учтён.")

    results: list[ContractResult] = []
    linked_names: set[str] = set()

    for key, group in contracts_docs.items():
        contract = group[0]
        flags: list[str] = []
        if len(group) > 1:
            flags.append(
                "Договор с этим номером загружен несколько раз ("
                + ", ".join(g.name for g in group)
                + ") — использован первый, требуется проверка."
            )
        if contract.signed_by_both is not True:
            flags.append(f"{contract.name}: в статусе договора нет отметки о подписании обеими сторонами.")

        supplier_ev = first_mention(contract.paragraphs, SUPPLIER)

        # базовые значения из договора
        amount = FieldValue()
        v, ev = _single(find_amounts(contract.paragraphs), "amount", contract, flags)
        if v is not None:
            amount = FieldValue(v, ev, contract, history=[(contract.label, v, ev)])
        else:
            amount.note_ev = first_mention(contract.paragraphs, MENTIONS_PRICE)
            amount.note = "Числовое значение общей стоимости в тексте не найдено."

        delivery = FieldValue()
        v, ev = _single(find_deliveries(contract.paragraphs), "delivery", contract, flags)
        if v is not None:
            delivery = FieldValue(v, ev, contract, history=[(contract.label, v, ev)])
        else:
            delivery.note_ev = first_mention(contract.paragraphs, MENTIONS_DELIVERY)
            delivery.note = "Срок поставки в тексте не найден."

        # связанные документы
        number = contract.number or contract.name
        related = [
            d
            for d in docs
            if d is not contract
            and d.kind in ("amendment", "draft", "letter")
            and any(r.upper() == number.upper() for r in d.contract_refs)
        ]
        res = ContractResult(number, contract, contract.supplier, supplier_ev, amount, delivery, related, flags=flags)

        applicable: list[Document] = []
        for d in related:
            linked_names.add(d.name)
            if len(d.contract_refs) > 1:
                flags.append(f"{d.name}: документ ссылается на несколько договоров ({', '.join(d.contract_refs)}) — проверьте привязку.")
            if contract.supplier and d.supplier and d.supplier != contract.supplier:
                flags.append(f"{d.name}: поставщик «{d.supplier}» не совпадает с поставщиком по договору — проверьте привязку.")

            if d.kind == "amendment":
                if d.signed_by_both is None:
                    flags.append(f"{d.name}: у допсоглашения не указан статус подписания — изменения не применены, требуется проверка.")
                    reason = "статус подписания не указан"
                elif not d.signed_by_both:
                    reason = "не подписано обеими сторонами"
                elif d.effective is None:
                    flags.append(f"{d.name}: в подписанном допсоглашении не найдена дата вступления в силу — изменения не применены, требуется проверка.")
                    reason = "не указана дата вступления в силу"
                elif d.effective > check_date:
                    reason = f"подписано, вступает в силу {fmt_date(d.effective)} — после даты проверки"
                else:
                    applicable.append(d)
                    continue
            elif d.kind == "draft":
                reason = "проект, не подписан"
            else:
                reason = "предложение в письме, не оформлено подписанным допсоглашением"
            _add_pending(res, d, reason)

        # применяем вступившие ДС по дате вступления в силу (не по имени файла)
        applicable.sort(key=lambda d: (d.effective, d.doc_date or d.effective))
        for d in applicable:
            touched = False
            for fname in ("amount", "delivery"):
                found = find_amounts(d.paragraphs) if fname == "amount" else find_deliveries(d.paragraphs)
                v, ev = _single(found, fname, d, flags)
                if v is None:
                    continue
                touched = True
                fv: FieldValue = getattr(res, fname)
                same_day = [h for h in applicable if h is not d and h.effective == d.effective]
                if same_day and fv.changed and fv.value != v and fv.source in same_day:
                    flags.append(
                        f"{d.name} и {fv.source.name} вступают в силу в один день и задают разные значения — требуется проверка."
                    )
                fv.value, fv.evidence, fv.source, fv.changed = v, ev, d, True
                fv.note = fv.note_ev = None
                fv.history.append((d.label, v, ev))
            if not touched and any(CHANGE_WORDS.search(p.text) for p in d.paragraphs):
                flags.append(f"{d.name}: допсоглашение содержит изменения, но сумма/срок в нём не распознаны — проверьте вручную.")

        results.append(res)

    for d in docs:
        if d.kind in ("amendment", "draft", "letter") and d.name not in linked_names:
            refs = ", ".join(d.contract_refs) or "номер не найден"
            warnings.append(f"{d.name}: не удалось связать с загруженным договором ({refs}).")

    results.sort(key=lambda r: r.number)
    return Analysis(check_date, docs, results, warnings)


def _add_pending(res: ContractResult, d: Document, reason: str) -> None:
    for v, ev in find_amounts(d.paragraphs):
        res.pending.append(PendingChange("amount", v, reason, d, ev))
    for v, ev in find_deliveries(d.paragraphs):
        res.pending.append(PendingChange("delivery", v, reason, d, ev))


# ---------------------------------------------------------------- вопросы пользователя


@dataclass
class Answer:
    text: str  # краткий ответ (markdown)
    contracts: list[ContractResult]
    fields: list[str]  # какие поля показывать с источниками


def deliveries_before(a: Analysis, limit: date) -> tuple[list[ContractResult], list[ContractResult]]:
    """Договоры со сроком поставки раньше limit и договоры без срока."""
    before = [c for c in a.contracts if c.delivery.value is not None and c.delivery.value < limit]
    unknown = [c for c in a.contracts if c.delivery.value is None]
    return before, unknown


def answer_question(q: str, a: Analysis) -> Answer:
    """Разбор простого текстового запроса правилами: номер договора + поле или «раньше <дата>»."""
    ql = q.lower()
    numbers = [n for n in CONTRACT_REF.findall(q.upper())]
    dates = [parse_date(x) for x in re.findall(DATE, q)]
    dates = [d for d in dates if d]
    if not dates:
        dates = _textual_dates(ql, a.check_date.year)

    wants_amount = bool(re.search(r"сумм|стоимост|цен", ql))
    wants_delivery = bool(re.search(r"срок|постав|дат", ql))

    if re.search(r"раньше|до\s|ранее|прежде", ql) and dates and not numbers:
        limit = dates[0]
        before, unknown = deliveries_before(a, limit)
        lines = [f"**Срок поставки раньше {fmt_date(limit)}** (на {fmt_date(a.check_date)}):"]
        if not before:
            lines.append("- таких договоров нет")
        for c in before:
            pend = c.pending_for("delivery")
            tail = (
                "; **есть несогласованные изменения срока**: "
                + "; ".join(f"{fmt_date(p.value)} ({p.doc.label}, {p.reason})" for p in pend)
                if pend
                else "; несогласованных изменений срока нет"
            )
            lines.append(f"- {c.number} — {fmt_date(c.delivery.value)}{tail}")
        if unknown:
            lines.append("- срок неизвестен (нет данных): " + ", ".join(c.number for c in unknown))
        others = [c for c in a.contracts if c not in before and c not in unknown and c.pending_for("delivery")]
        if others:
            lines.append(
                "\nНесогласованные изменения срока есть также по: "
                + ", ".join(f"{c.number} (предлагается {', '.join(fmt_date(p.value) for p in c.pending_for('delivery'))})" for c in others)
            )
        return Answer("\n".join(lines), before + others, ["delivery"])

    if not numbers:
        return Answer(
            "Не понял запрос. Укажите номер договора (например, «сумма STN-102») "
            "или спросите «какие поставки раньше 01.04.2026».",
            [],
            [],
        )

    fields = [f for f, wanted in (("amount", wants_amount), ("delivery", wants_delivery)) if wanted]
    if not fields:
        fields = ["amount", "delivery"]
    lines, found = [], []
    for n in numbers:
        c = a.get(n)
        if c is None:
            lines.append(f"- Договор {n} среди загруженных документов не найден.")
            continue
        found.append(c)
        for f in fields:
            fv: FieldValue = getattr(c, f)
            name = "Сумма" if f == "amount" else "Срок поставки"
            val = fmt_value(f, fv.value)
            src = ""
            if fv.value is not None:
                src = f" — по документу «{fv.source.label}» ({fv.evidence.doc})"
                if fv.changed:
                    src += ", изменено допсоглашением"
            elif fv.note:
                src = f" — {fv.note}"
            lines.append(f"- **{c.number}. {name}: {val}**{src}")
            for p in c.pending_for(f):
                lines.append(f"  - не согласовано: {fmt_value(f, p.value)} — {p.doc.label} ({p.reason})")
    return Answer("\n".join(lines), found, fields)


MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6,
    "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}


def _textual_dates(ql: str, year: int) -> list[date]:
    """«1 апреля 2026» -> date."""
    out = []
    for m in re.finditer(r"(\d{1,2})\s+([а-яё]+)(?:\s+(\d{4}))?", ql):
        word = m.group(2)
        for stem, num in sorted(MONTHS.items(), key=lambda kv: -len(kv[0])):
            if word.startswith(stem) and (stem != "ма" or word in ("мая", "май")):
                try:
                    out.append(date(int(m.group(3) or year), num, int(m.group(1))))
                except ValueError:
                    pass
                break
    return out
