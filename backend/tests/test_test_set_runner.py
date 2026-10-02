"""The generated-set runner's own judgments (validation/test_set.py): what
it counts as the same product, how it finds a line's plan row, and what it
says about a set before anything is read."""
import csv

from validation.test_set import _matching, check_set, same_product


def test_a_product_is_the_same_only_when_one_names_words_are_all_in_the_others():
    assert same_product("Chicken Breast Boneless Skinless", "Chicken breast")
    assert same_product("Cups 16oz Hot", "hot cups 16 oz")
    assert same_product("Eggs Large", "Eggs large brown")  # the catalog doesn't tell brown from white
    assert not same_product("Chicken Breast Boneless Skinless", "Chicken thigh boneless skinless")
    assert not same_product("Cups 16oz Hot", "cold cups 16 oz")
    assert not same_product("2% Milk", "Whole milk")
    assert not same_product("Unsalted Butter", "Salted butter")
    assert not same_product("Ground Beef 80/20", "Ground beef 90/10")
    assert not same_product(None, "Chicken breast")
    assert same_product("Tomato Roma", "Roma tomatoes") and same_product("Potato Russet", "Russet potatoes")


def _line(code, description, product, status="auto"):
    return {"item_code": code, "description": description, "status": status, "product": product, "confidence": "0.95",
            "pack": "4/10 LB", "uom": "CS", "price_per_unit": "2.5", "unit": "lb"}  # fmt: skip


def test_one_code_on_two_rows_is_judged_row_by_row():
    """A contract price and a market price of one code, or a code reused for
    another product: each line against its own row of the plan."""
    plan = [
        {"invoice_file": "V-01.pdf", "item_code": "111", "description": "CHKN BRST BNLS", "true_product": "Chicken breast", "true_pack": ""},
        {"invoice_file": "V-01.pdf", "item_code": "111", "description": "CHKN THIGH BNLS", "true_product": "Chicken thigh", "true_pack": ""},
        {"invoice_file": "V-01.pdf", "item_code": "", "description": "GIFT CARD", "true_product": "none", "true_pack": ""},
    ]
    lines = {
        "V-01": [
            _line("111", "CHKN BRST BNLS", "Chicken Breast Boneless Skinless"),
            _line("111", "chkn  thigh bnls", "Chicken Breast Boneless Skinless"),  # the trap, fallen into
            _line("", "GIFT CARD", None, status="pending"),
            _line("999", "NOT IN THE PLAN", "Anything"),
        ]
    }
    out = _matching(plan, lines)
    assert (out["lines"], out["right"], out["left_for_a_person"], out["not_in_plan"]) == (3, 1, 1, 1)
    assert len(out["wrong_automatic"]) == 1 and "thigh" in out["wrong_automatic"][0].lower()


def _write(path, rows):
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def test_a_set_is_checked_before_anything_is_read(tmp_path):
    manifest = [
        {"file": "U-01.pdf", "set": "U", "distributor": "sysco", "customer": "Marigold Diner", "account": "",
         "expected_outcome": "Ready", "expected_reason": "", "certainty": "known", "upload_to": "", "notes": ""},
        {"file": "U-02.pdf", "set": "U", "distributor": "sysco", "customer": "Marigold Diner", "account": "",
         "expected_outcome": "Ready", "expected_reason": "", "certainty": "known", "upload_to": "", "notes": ""},
        {"file": "P-01.pdf", "set": "P", "distributor": "other", "customer": "Harbor & Pine Kitchen", "account": "",
         "expected_outcome": "Ready", "expected_reason": "", "certainty": "maybe", "upload_to": "", "notes": ""},
        {"file": "W-01.pdf", "set": "W", "distributor": "other", "customer": "Harbor & Pine Kitchen", "account": "",
         "expected_outcome": "Needs a look or Rejected", "expected_reason": "", "certainty": "find_out", "upload_to": "", "notes": ""},
        {"file": "W-02.pdf", "set": "W", "distributor": "other", "customer": "Harbor & Pine Kitchen", "account": "",
         "expected_outcome": "Exploded", "expected_reason": "", "certainty": "find_out", "upload_to": "", "notes": ""},
        {"file": "T-01.pdf", "set": "T", "distributor": "sysco", "customer": "Harbor Pine Kitchen & Bar", "account": "",
         "expected_outcome": "Needs a look", "expected_reason": "", "certainty": "known", "upload_to": "", "notes": ""},
        {"file": "T-02.pdf", "set": "T", "distributor": "sysco", "customer": "Harbor & Pine Kitchen", "account": "",
         "expected_outcome": "Needs a look", "expected_reason": "", "certainty": "known", "upload_to": "Queen City Diner", "notes": ""},
    ]  # fmt: skip
    _write(tmp_path / "manifest.csv", manifest)
    for name in ("U-01", "U-02", "P-01", "W-01", "T-01", "T-02"):  # W-02's file is missing
        (tmp_path / f"{name}.pdf").write_bytes(b"%PDF-1.4")
    for name in ("P-01", "T-01", "T-02"):  # U-02 has no key
        (tmp_path / f"{name}.truth.json").write_text("{}")
    from datetime import date, timedelta

    later = date.today() + timedelta(days=120)  # a series that ends after today
    (tmp_path / "U-01.truth.json").write_text(f'{{"invoice_date": "{later}"}}')
    _write(
        tmp_path / "vendors_plan.csv",
        [
            {"vendor": "Hollow Creek Farm", "restaurant": "Harbor & Pine Kitchen", "first_invoice_file": "P-01.pdf"},
            {"vendor": "gordon", "restaurant": "Harbor & Pine Kitchen", "first_invoice_file": "W-01.pdf"},
        ],
    )
    _write(
        tmp_path / "restaurants.csv",
        [
            {"customer": "Marigold Diner", "city": "Tulsa", "state": "OK", "account": "", "sets": "U;A"},
            {"customer": "Harbor & Pine Kitchen", "city": "Charlotte", "state": "NC", "account": "", "sets": "P;T;W"},
        ],
    )
    plan = {"week": "1", "item_code": "1", "description": "X", "pack_size": "4/5 LB", "unit_price": "1", "uom": "CS"}
    _write(
        tmp_path / "year_plan.csv",
        [
            {**plan, "invoice_file": "U-01.pdf", "behavior": "inflation", "expected_alert": "yes", "upload_order": "2"},
            {**plan, "invoice_file": "U-02.pdf", "behavior": "inflation", "expected_alert": "no", "upload_order": ""},
            {**plan, "invoice_file": "U-03.pdf", "behavior": "promo", "expected_alert": "maybe", "upload_order": "2"},
        ],
    )

    problems = "\n".join(check_set(tmp_path))
    for expected in (
        "W-02.pdf: in the manifest, not in the folder",
        "'Exploded' isn't one the runner knows",
        "P-01.pdf: certainty 'maybe'",
        "U-02: expected Ready but has no answer key",
        "Hollow Creek Farm's first invoice at Harbor & Pine Kitchen is expected Ready",
        "Marigold Diner: in sets ['A', 'U']",
        "year_plan: 1 files without one whole-number upload_order",
        "year_plan: two files share an upload_order",
        "inflation rows disagree on expected_alert",
        "promo has expected_alert ['maybe']; it must be yes, no or find_out",
        "year_plan.csv: 1 files not in the manifest",
        # What the sixth set's own files got wrong.
        "1 documents are billed to a name that isn't in restaurants.csv and have no upload_to, e.g. ['T-01.pdf']",
        "upload_to names restaurants that aren't in restaurants.csv: ['Queen City Diner']",
        "vendors_plan: 1 rows name a big distributor",
        f"the price series is dated up to {later}, after today",
    ):
        assert expected in problems, (expected, problems)
    assert "W-01" not in problems  # "Needs a look or Rejected" is an outcome it knows


def test_an_increase_that_closed_before_the_end_still_alerted_at_the_time():
    """A step in March has closed by December. Graded only on what is open
    at the end, the plan called that a miss."""
    from datetime import date, timedelta
    from decimal import Decimal

    from validation.test_set import alerted_at_some_point

    def weekly(prices):
        return [(date(2026, 1, 5) + timedelta(weeks=week), Decimal(price)) for week, price in enumerate(prices)]

    assert alerted_at_some_point(weekly(["5.00"] * 10 + ["5.60"] * 30))
    assert not alerted_at_some_point(weekly(["5.00"] * 40))
    assert not alerted_at_some_point(weekly(["5.00"] * 10 + ["4.40"] * 5 + ["5.00"] * 25))  # a promotion
    assert not alerted_at_some_point([])


def test_a_row_the_app_restates_to_what_the_key_holds_is_not_a_misreading(tmp_path):
    """A catch weight printed in the pack column: the reader gives the
    cases as printed, the app restates the row by the pound, and the key
    holds the pounds. Scored as read, three right invoices were listed as
    "read with a wrong amount and still Ready"."""
    import json

    from validation.test_set import score

    row = {"line_number": 1, "raw_sku": "1", "raw_description": "BEEF BRISKET", "raw_pack_size": "47.30 LB", "uom": "CS",
           "unit_price": "5.10", "extended_price": "723.69", "confidence": 1.0}  # fmt: skip
    head = {"distributor": "us_foods", "invoice_number": "1", "invoice_date": "2026-09-04", "subtotal": "723.69", "tax": "0.00", "total": "723.69"}
    (tmp_path / "gate.json").write_text(json.dumps({"S-30": {"set": "S", "outcome": None}}))
    (tmp_path / "S-30.extracted.json").write_text(
        json.dumps({"cost_usd": 0.03, "failed": None, "extraction": {**head, "line_items": [{**row, "quantity": "3"}]}})
    )
    (tmp_path / "S-30.truth.json").write_text(json.dumps({**head, "line_items": [{**row, "quantity": "141.90", "uom": "LB"}]}))
    assert score(tmp_path)["S-30"]["money_mistake"] is False
    # A quantity that is simply wrong is still a misreading.
    (tmp_path / "S-30.extracted.json").write_text(
        json.dumps({"cost_usd": 0.03, "failed": None, "extraction": {**head, "line_items": [{**row, "quantity": "4"}]}})
    )
    assert score(tmp_path)["S-30"]["money_mistake"] is True


def test_a_row_that_fits_two_products_is_right_when_left_for_a_person():
    from validation.test_set import _matching

    plan = [
        {"invoice_file": "V-01.pdf", "item_code": "1", "description": "CHEESE MOZZ SHRD",
         "true_product": "either: Mozzarella Shredded Whole Milk / Mozzarella Shredded Part Skim", "true_pack": "", "note": ""},
        {"invoice_file": "V-01.pdf", "item_code": "2", "description": "CONT TO GO",
         "true_product": "either: To-Go Container 8oz / To-Go Container 32oz", "true_pack": "", "note": ""},
    ]  # fmt: skip
    line = {"status": "pending", "confidence": "0.7", "pack": "4/5 LB", "uom": "CS", "price_per_unit": None, "unit": None}
    matched = {
        "V-01": [
            {**line, "item_code": "1", "description": "CHEESE MOZZ SHRD", "product": None},
            {**line, "item_code": "2", "description": "CONT TO GO", "product": "To-Go Container 8oz"},
        ]
    }
    result = _matching(plan, matched)
    assert result["right"] == 1 and len(result["wrong_suggested"]) == 1 and "CONT TO GO" in result["wrong_suggested"][0]
