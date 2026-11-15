from datetime import date
from pathlib import Path

import pytest

from extractor import analyze, answer_question, decode

DOCS = Path(__file__).parent.parent / "documents"
CHECK = date(2026, 3, 10)


def load():
    return {p.name: decode(p.read_bytes()) for p in sorted(DOCS.glob("*.txt"))}


@pytest.fixture
def files():
    return load()


@pytest.fixture
def a(files):
    return analyze(files, CHECK)


def rub(n):
    return n * 100


CONTRACT_TEMPLATE = """ДОГОВОР ПОСТАВКИ № {num}
Дата: 01.02.2026
Статус: {status}

1.1. Покупатель: ООО «СеверМет». Поставщик: {supplier}.
1.2. Предмет: поставка насосов.

2.1. Общая стоимость поставки составляет {amount} рублей, включая НДС. Указанная сумма — итоговая стоимость договора.
2.2. Авансовый платеж составляет 100 000 рублей и входит в общую стоимость по пункту 2.1.

3.1. Поставщик обязуется поставить насосы не позднее {delivery}.
3.2. Оплата производится не позднее 30.06.2026. Этот срок является сроком оплаты, а не поставки.
"""

AMENDMENT_TEMPLATE = """ДОПОЛНИТЕЛЬНОЕ СОГЛАШЕНИЕ № {n} К ДОГОВОРУ {num}
Дата: {d}
{status_line}
Покупатель: ООО «СеверМет». Поставщик: ООО «Тест».

1. Пункт 3.1 договора изложить в следующей редакции: «Поставщик обязуется поставить насосы не позднее {delivery}».
{effective_line}
"""


def contract(num="STN-200", amount="2 345 678", delivery="15.05.2026",
             status="подписан обеими сторонами 01.02.2026.", supplier="ООО «Тест»"):
    return CONTRACT_TEMPLATE.format(num=num, amount=amount, delivery=delivery, status=status, supplier=supplier)


def amendment(num="STN-200", n=1, d="01.03.2026", delivery="20.06.2026",
              status="подписано обеими сторонами 01.03.2026.", effective="01.03.2026"):
    return AMENDMENT_TEMPLATE.format(
        n=n, num=num, d=d, delivery=delivery,
        status_line=f"Статус: {status}" if status else "",
        effective_line=f"2. Настоящее соглашение вступает в силу {effective}." if effective else "",
    )


# ------------------------------------------------ четыре вопроса задания


def test_q1_stn101_delivery_changed_by_signed_amendment(a):
    c = a.get("STN-101")
    assert c.delivery.value == date(2026, 4, 30)
    assert c.delivery.changed
    assert c.delivery.evidence.doc == "02_amendment_signed.txt"
    assert "30.04.2026" in c.delivery.evidence.quote
    # проект ДС № 2 не применён, но показан отдельно
    pend = c.pending_for("delivery")
    assert [p.value for p in pend] == [date(2026, 5, 10)]
    assert pend[0].doc.name == "06_amendment_draft.txt"


def test_stn101_amount_from_signed_amendment_not_draft(a):
    c = a.get("STN-101")
    assert c.amount.value == rub(1_380_000)
    assert [h[1] for h in c.amount.history] == [rub(1_200_000), rub(1_380_000)]
    assert [p.value for p in c.pending_for("amount")] == [rub(1_450_000)]


def test_q2_stn102_amount_does_not_add_advance(a):
    c = a.get("STN-102")
    assert c.amount.value == rub(840_000)
    assert not c.amount.changed
    assert c.amount.evidence.paragraph.line == 8


def test_stn102_delivery_not_payment_date_and_letter_is_pending(a):
    c = a.get("STN-102")
    assert c.delivery.value == date(2026, 3, 20)  # не 10.04 (оплата) и не 27.03 (письмо)
    pend = c.pending_for("delivery")
    assert len(pend) == 1 and pend[0].value == date(2026, 3, 27)
    assert pend[0].doc.kind == "letter"


def test_q3_stn103_amount_no_data_not_budget_limit(a):
    c = a.get("STN-103")
    assert c.amount.value is None  # 600 000 — лимит, не стоимость
    assert c.amount.note_ev is not None and c.amount.note_ev.paragraph.line == 8
    assert c.delivery.value == date(2026, 4, 30)


def test_q4_deliveries_before_april(a):
    ans = answer_question(
        "По каким договорам поставка запланирована раньше 1 апреля 2026 года и есть ли несогласованные изменения срока?", a
    )
    assert [c.number for c in ans.contracts][:1] == ["STN-102"]
    assert "STN-102 — 20.03.2026" in ans.text
    assert "27.03.2026" in ans.text
    assert "STN-101 —" not in ans.text and "STN-103 —" not in ans.text


def test_text_answers(a):
    assert "30.04.2026" in answer_question("Какой срок поставки действует по договору STN-101 на 10 марта 2026 года?", a).text
    assert "840 000 ₽" in answer_question("Какова сумма договора STN-102?", a).text
    assert "нет данных" in answer_question("Какова сумма договора STN-103?", a).text
    assert "не найден" in answer_question("сумма STN-999", a).text


def test_suppliers(a):
    assert {c.number: c.supplier for c in a.contracts} == {
        "STN-101": "ООО «Вектор Прибор»",
        "STN-102": "ООО «Контур Электро»",
        "STN-103": "ООО «ПромСнаб Сервис»",
    }
    assert not a.warnings
    assert all(not c.flags for c in a.contracts)


# ------------------------------------------------ устойчивость к подмене данных


def test_file_names_do_not_define_priority(files):
    renamed = {
        "zz_contract.txt": files["01_contract.txt"],
        "99_FINAL_signed_amendment.txt": files["06_amendment_draft.txt"],  # проект под «важным» именем
        "00_old.txt": files["02_amendment_signed.txt"],
    }
    c = analyze(renamed, CHECK).get("STN-101")
    assert c.delivery.value == date(2026, 4, 30)
    assert c.amount.value == rub(1_380_000)


def test_new_contract_same_format_is_parsed():
    a = analyze({"new.txt": contract(amount="2 345 678,50", delivery="15.05.2026")}, CHECK)
    c = a.get("STN-200")
    assert c.amount.value == 234_567_850
    assert c.delivery.value == date(2026, 5, 15)  # не 30.06 (оплата)
    assert c.supplier == "ООО «Тест»"


def test_changed_field_in_existing_contract(files):
    files["01_contract.txt"] = files["01_contract.txt"].replace("15.04.2026", "12.04.2026")
    files["02_amendment_signed.txt"] = files["02_amendment_signed.txt"].replace("1 380 000", "1 390 500")
    a = analyze(files, CHECK)
    c = a.get("STN-101")
    assert c.amount.value == rub(1_390_500)
    assert c.delivery.history[0][1] == date(2026, 4, 12)


def test_amendment_signed_by_one_side_is_not_applied():
    a = analyze({"c.txt": contract(), "a.txt": amendment(status="подписано поставщиком 01.03.2026.")}, CHECK)
    c = a.get("STN-200")
    assert c.delivery.value == date(2026, 5, 15)
    assert c.pending_for("delivery")[0].value == date(2026, 6, 20)


def test_amendment_effective_after_check_date():
    files = {"c.txt": contract(), "a.txt": amendment(effective="01.04.2026")}
    c = analyze(files, CHECK).get("STN-200")
    assert c.delivery.value == date(2026, 5, 15)
    assert "после даты проверки" in c.pending_for("delivery")[0].reason
    # после наступления даты — применяется
    c2 = analyze(files, date(2026, 4, 2)).get("STN-200")
    assert c2.delivery.value == date(2026, 6, 20) and c2.delivery.changed


def test_amendment_without_status_or_effective_date_needs_review():
    c = analyze({"c.txt": contract(), "a.txt": amendment(status=None)}, CHECK).get("STN-200")
    assert c.delivery.value == date(2026, 5, 15)
    assert any("статус" in f for f in c.flags)
    c = analyze({"c.txt": contract(), "a.txt": amendment(effective=None)}, CHECK).get("STN-200")
    assert c.delivery.value == date(2026, 5, 15)
    assert any("дата вступления" in f for f in c.flags)


def test_amendments_applied_by_effective_date_not_file_order():
    files = {
        "a_second.txt": amendment(n=2, d="05.03.2026", delivery="25.06.2026",
                                  status="подписано обеими сторонами 05.03.2026.", effective="05.03.2026"),
        "b_first.txt": amendment(n=1, delivery="20.06.2026"),
        "c.txt": contract(),
    }
    c = analyze(files, CHECK).get("STN-200")
    assert c.delivery.value == date(2026, 6, 25)
    assert [h[1] for h in c.delivery.history] == [date(2026, 5, 15), date(2026, 6, 20), date(2026, 6, 25)]


def test_conflicting_values_in_contract_need_review():
    text = contract() + "\n2.3. Общая стоимость поставки составляет 999 000 рублей.\n"
    c = analyze({"c.txt": text}, CHECK).get("STN-200")
    assert c.amount.value is None
    assert any("несколько разных значений" in f for f in c.flags)


def test_unlinked_document_warning():
    a = analyze({"c.txt": contract(), "a.txt": amendment(num="STN-777")}, CHECK)
    assert any("не удалось связать" in w for w in a.warnings)
    assert a.get("STN-200").delivery.value == date(2026, 5, 15)


def test_missing_delivery_is_no_data():
    text = contract().replace("3.1. Поставщик обязуется поставить насосы не позднее 15.05.2026.", "3.1. Срок согласуется дополнительно.")
    c = analyze({"c.txt": text}, CHECK).get("STN-200")
    assert c.delivery.value is None


def test_cp1251_decoding():
    raw = contract().encode("cp1251")
    a = analyze({"c.txt": decode(raw)}, CHECK)
    assert a.get("STN-200").amount.value == rub(2_345_678)
