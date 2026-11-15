"""Streamlit-интерфейс: проверка условий договоров поставки.

Запуск:  streamlit run app.py
"""
from __future__ import annotations

import html
from datetime import date
from pathlib import Path

import streamlit as st

from extractor import (
    Analysis,
    ContractResult,
    Document,
    Evidence,
    FieldValue,
    analyze,
    answer_question,
    fmt_value,
)
from loader import collect_files

DEFAULT_CHECK_DATE = date(2026, 3, 10)
SAMPLE_DIR = Path(__file__).parent / "documents"
FIELD_NAMES = {"amount": "Сумма", "delivery": "Срок поставки"}
PRESET_QUESTIONS = [
    "Какой срок поставки действует по договору STN-101 на 10 марта 2026 года?",
    "Какова сумма договора STN-102?",
    "Какова сумма договора STN-103?",
    "По каким договорам поставка запланирована раньше 1 апреля 2026 года и есть ли несогласованные изменения срока?",
]

st.set_page_config(page_title="Проверка договоров поставки", layout="wide")

st.markdown(
    """
<style>
.ev {border-left: 3px solid #4a7bd0; padding: 6px 10px; margin: 4px 0 10px; background: rgba(74,123,208,.06); border-radius: 4px;}
.ev .meta {font-size: .82em; opacity: .75;}
.ev.pending {border-left-color: #d08a1e; background: rgba(208,138,30,.08);}
.ev.missing {border-left-color: #999; background: rgba(150,150,150,.08);}
.src p {margin: 2px 0; padding: 3px 8px; border-radius: 4px; scroll-margin-top: 80px;}
.src p:target {background: #ffe89a; color: #111; outline: 2px solid #e0b400;}
.src .ln {display: inline-block; width: 2.4em; opacity: .5; font-size: .8em;}
table.summary {border-collapse: collapse; width: 100%;}
table.summary th, table.summary td {border-bottom: 1px solid rgba(128,128,128,.3); padding: 6px 8px; text-align: left; vertical-align: top;}
.badge {display: inline-block; padding: 1px 7px; border-radius: 10px; font-size: .8em; margin: 1px 2px 1px 0;}
.b-ok {background: #dff3e3; color: #1d6b31;}
.b-chg {background: #dde8fb; color: #1f4f9c;}
.b-pend {background: #fbecd3; color: #8a560b;}
.b-warn {background: #fbdada; color: #9c1f1f;}
.b-none {background: #e8e8e8; color: #555;}
mark {background: #ffe89a; color: inherit; padding: 0 1px;}
</style>
""",
    unsafe_allow_html=True,
)

# ---------------------------------------------------------------- helpers


def e(s: str) -> str:
    return html.escape(s, quote=True)


def quote_html(ev: Evidence) -> str:
    text, frag = ev.quote, ev.match
    i = text.find(frag)
    if i < 0:
        return e(text)
    return e(text[:i]) + "<mark>" + e(frag) + "</mark>" + e(text[i + len(frag):])


def link(ev: Evidence, label: str | None = None) -> str:
    p = ev.paragraph
    return f'<a href="#{p.anchor}" target="_self">{e(label or f"{p.doc}, стр. {p.line}")} ↗</a>'


def evidence_block(ev: Evidence, title: str, css: str = "") -> str:
    return (
        f'<div class="ev {css}"><div class="meta">{e(title)} · {link(ev)}</div>'
        f"«{quote_html(ev)}»</div>"
    )


def field_cell(fv: FieldValue, field: str, c: ContractResult) -> str:
    out = f"<b>{e(fmt_value(field, fv.value))}</b>"
    if fv.value is None:
        out += ' <span class="badge b-none">нет данных</span>'
    elif fv.changed:
        out += f' <span class="badge b-chg">изменено: {e(fv.source.label)}</span>'
    if fv.evidence:
        out += "<br>" + link(fv.evidence, "источник")
    for p in c.pending_for(field):
        out += (
            f'<br><span class="badge b-pend">не согласовано: {e(fmt_value(field, p.value))}</span> '
            + link(p.evidence, p.doc.label)
        )
    return out


def summary_table(a: Analysis) -> str:
    rows = []
    for c in a.contracts:
        status = (
            f'<span class="badge b-warn">требует проверки ({len(c.flags)})</span>'
            if c.flags
            else '<span class="badge b-ok">разрешено</span>'
        )
        supplier = e(c.supplier or "нет данных")
        if c.supplier_ev:
            supplier += "<br>" + link(c.supplier_ev, "источник")
        rows.append(
            f"<tr><td><b>{e(c.number)}</b><br><small>{e(c.contract.name)}</small></td>"
            f"<td>{supplier}</td>"
            f"<td>{field_cell(c.amount, 'amount', c)}</td>"
            f"<td>{field_cell(c.delivery, 'delivery', c)}</td>"
            f"<td>{status}</td></tr>"
        )
    return (
        '<table class="summary"><tr><th>Договор</th><th>Поставщик</th><th>Сумма</th>'
        "<th>Срок поставки</th><th>Статус</th></tr>" + "".join(rows) + "</table>"
    )


def field_details(c: ContractResult, field: str) -> str:
    fv: FieldValue = getattr(c, field)
    name = FIELD_NAMES[field]
    parts = []
    if fv.value is not None:
        how = f"изменено документом «{fv.source.label}»" if fv.changed else f"по документу «{fv.source.label}»"
        parts.append(evidence_block(fv.evidence, f"{name}: {fmt_value(field, fv.value)} — {how}"))
        if len(fv.history) > 1:
            hist = " → ".join(f"{fmt_value(field, v)} ({lbl})" for lbl, v, _ in fv.history)
            parts.append(f'<div class="meta" style="font-size:.85em;margin:-4px 0 8px">История: {e(hist)}</div>')
    else:
        msg = f"{name}: нет данных. {fv.note or ''}"
        if fv.note_ev:
            parts.append(evidence_block(fv.note_ev, msg + " Ближайший по смыслу абзац:", "missing"))
        else:
            parts.append(f'<div class="ev missing">{e(msg)}</div>')
    for p in c.pending_for(field):
        parts.append(
            evidence_block(
                p.evidence,
                f"Не согласовано: {fmt_value(field, p.value)} — {p.doc.label} ({p.reason})",
                "pending",
            )
        )
    return "".join(parts)


def doc_badge(d: Document) -> str:
    if d.kind == "contract":
        return '<span class="badge b-ok">договор</span>'
    if d.kind == "amendment":
        if d.signed_by_both and d.effective:
            return f'<span class="badge b-chg">ДС подписано, в силе с {d.effective:%d.%m.%Y}</span>'
        return '<span class="badge b-pend">ДС не действует</span>'
    if d.kind == "draft":
        return '<span class="badge b-pend">проект — не подписан</span>'
    if d.kind == "letter":
        return '<span class="badge b-pend">письмо — не изменяет условия</span>'
    return '<span class="badge b-warn">не распознан</span>'


def source_html(d: Document) -> str:
    paras = "".join(
        f'<p id="{p.anchor}"><span class="ln">{p.line}</span>{e(p.text)}</p>' for p in d.paragraphs
    )
    return f'<div class="src">{paras}</div>'


# ---------------------------------------------------------------- sidebar


with st.sidebar:
    st.header("Документы")
    uploaded_files = st.file_uploader(
        "Файлы или ZIP-архивы",
        type=["txt", "zip"],
        accept_multiple_files=True,
        help="Можно выбрать сразу несколько файлов (Ctrl+клик) или перетащить их группой. "
        "Из ZIP извлекаются все TXT, включая вложенные папки.",
    )
    uploaded_dir = st.file_uploader(
        "Или папка целиком",
        type=["txt", "zip"],
        accept_multiple_files="directory",
        help="Загружаются все TXT и ZIP из папки и её подпапок.",
    )
    uploaded = list(uploaded_files or []) + list(uploaded_dir or [])
    use_sample = st.checkbox(
        "Использовать учебный комплект из папки documents/",
        value=not uploaded,
        disabled=bool(uploaded),
        help="Если загружены свои файлы, учебный комплект не используется.",
    )
    check_date = st.date_input("Дата проверки", value=DEFAULT_CHECK_DATE, format="DD.MM.YYYY")
    st.caption(
        "Условия меняет только допсоглашение, подписанное обеими сторонами и вступившее "
        "в силу к дате проверки. Письма и проекты показываются отдельно."
    )

load_notes: list[str] = []
if uploaded:
    files, load_notes = collect_files([(f.name, f.getvalue()) for f in uploaded])
elif use_sample:
    files, load_notes = collect_files([(p.name, p.read_bytes()) for p in sorted(SAMPLE_DIR.glob("*.txt"))])
else:
    files = {}

st.title("Проверка условий договоров поставки")

if not files:
    for n in load_notes:
        st.warning(n)
    st.info("Загрузите TXT-файлы, ZIP-архив или папку в панели слева либо включите учебный комплект.")
    st.stop()

a = analyze(files, check_date)

st.caption(
    f"Загружено документов: {len(files)} · договоров: {len(a.contracts)} · дата проверки: {check_date:%d.%m.%Y}"
)
with st.expander(f"Загруженные документы ({len(files)})", expanded=bool(load_notes)):
    st.markdown(
        "".join(f"<div>{e(d.name)} — {e(d.label)} {doc_badge(d)}</div>" for d in a.documents),
        unsafe_allow_html=True,
    )
    for n in load_notes:
        st.caption(n)
for w in a.warnings:
    st.warning(w)

# ---------------------------------------------------------------- сводная таблица

st.subheader("Сводная таблица")
if a.contracts:
    st.markdown(summary_table(a), unsafe_allow_html=True)
else:
    st.warning("Среди загруженных файлов не найдено ни одного договора.")

# ---------------------------------------------------------------- вопросы

st.subheader("Вопросы")
cols = st.columns(len(PRESET_QUESTIONS))
for i, (col, q) in enumerate(zip(cols, PRESET_QUESTIONS)):
    if col.button(q, key=f"q{i}", use_container_width=True):
        st.session_state["question"] = q
question = st.text_input(
    "Или задайте свой вопрос (номер договора + «сумма»/«срок», либо «поставки раньше ДД.ММ.ГГГГ»)",
    key="question",
)
if question:
    ans = answer_question(question, a)
    st.markdown(ans.text)
    with st.container(border=True):
        st.markdown("**Основания**")
        for c in ans.contracts:
            st.markdown(
                f"<b>{e(c.number)}</b>" + "".join(field_details(c, f) for f in ans.fields),
                unsafe_allow_html=True,
            )

# ---------------------------------------------------------------- детали по договорам

st.subheader("Детали и основания по договорам")
for c in a.contracts:
    with st.container(border=True):
        st.markdown(f"#### {c.number} — {c.supplier or 'поставщик: нет данных'}")
        st.markdown(field_details(c, "amount") + field_details(c, "delivery"), unsafe_allow_html=True)
        if c.related:
            st.markdown(
                "Связанные документы: "
                + ", ".join(f"{e(d.name)} {doc_badge(d)}" for d in c.related),
                unsafe_allow_html=True,
            )
        for f in c.flags:
            st.error(f"Требуется проверка: {f}")

# ---------------------------------------------------------------- исходные тексты

st.subheader("Исходные документы")
st.caption("Ссылки «↗» ведут сюда и подсвечивают нужный абзац.")
order = {"contract": 0, "amendment": 1, "draft": 2, "letter": 3, "unknown": 4}
for d in sorted(a.documents, key=lambda d: (d.contract_refs[:1] or [d.number or ""], order[d.kind], d.name)):
    st.markdown(f"**{e(d.name)}** — {e(d.label)} {doc_badge(d)}", unsafe_allow_html=True)
    st.markdown(source_html(d), unsafe_allow_html=True)
