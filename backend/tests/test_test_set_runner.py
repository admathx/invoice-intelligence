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
    ]  # fmt: skip
    _write(tmp_path / "manifest.csv", manifest)
    for name in ("U-01", "U-02", "P-01", "W-01"):  # W-02's file is missing
        (tmp_path / f"{name}.pdf").write_bytes(b"%PDF-1.4")
    for name in ("U-01", "P-01"):  # U-02 has no key
        (tmp_path / f"{name}.truth.json").write_text("{}")
    _write(tmp_path / "vendors_plan.csv", [{"vendor": "Hollow Creek Farm", "restaurant": "Harbor & Pine Kitchen", "first_invoice_file": "P-01.pdf"}])
    _write(tmp_path / "restaurants.csv", [{"customer": "Marigold Diner", "city": "Tulsa", "state": "OK", "account": "", "sets": "U;A"}])
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
        "promo has expected_alert ['maybe']",
        "year_plan.csv: 1 files not in the manifest",
    ):
        assert expected in problems, (expected, problems)
    assert "W-01" not in problems  # "Needs a look or Rejected" is an outcome it knows
