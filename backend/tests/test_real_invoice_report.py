"""The real-invoice harness (validation/real_invoice_report.py), exercised
with the fake extractor: each invoice is extracted once, only reviewed truth
counts, and a real money mistake is told apart from a correct extraction."""
import io
import json

from reportlab.pdfgen import canvas

from app.extract.client import FakeExtractorClient
from validation.real_invoice_report import extract_folder, score_folder


class CountingExtractor(FakeExtractorClient):
    calls = 0

    def extract(self, page_image_paths):
        CountingExtractor.calls += 1
        return super().extract(page_image_paths)


def _pdf(path, text):
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 720, text)
    c.showPage()
    c.save()
    path.write_bytes(buf.getvalue())


def test_extracts_once_scores_only_reviewed_and_separates_real_mistakes(tmp_path):
    for name in ("a", "b", "c"):
        _pdf(tmp_path / f"{name}.pdf", name)
    CountingExtractor.calls = 0

    assert len(extract_folder(tmp_path, CountingExtractor(), fake=True, limit=2)) == 2
    assert len(extract_folder(tmp_path, CountingExtractor(), fake=True, limit=None)) == 1
    assert extract_folder(tmp_path, CountingExtractor(), fake=True, limit=None) == []
    assert CountingExtractor.calls == 3  # each invoice paid for once

    # a: the reviewer found extraction exactly right. b: the page said
    # something different for line 1's unit price and extended price.
    # c: not reviewed yet.
    for name in ("a", "b"):
        path = tmp_path / f"{name}.fake-truth.json"
        truth = json.loads(path.read_text())
        assert truth["_reviewed"] is False
        truth["_reviewed"] = True
        if name == "b":
            line = truth["line_items"][0]
            line["unit_price"], line["extended_price"] = "9.99", "19.98"
        path.write_text(json.dumps(truth))

    summary = score_folder(tmp_path, fake=True)
    assert summary["scored"] == 2 and summary["awaiting_review"] == ["c.pdf"]
    assert summary["line_recall"] == 1.0
    assert summary["field_accuracy"]["unit_price"] < 1.0
    assert summary["invoices_with_money_mistakes"] == 1
    # The fake extraction's own numbers add up, so the check can't see b's
    # mistake: a miss, correctly reported as one.
    assert summary["mistakes_caught"] == 0 and summary["false_alarms"] == 0


def test_a_reviewer_edit_is_never_overwritten_by_re_extraction(tmp_path):
    _pdf(tmp_path / "a.pdf", "a")
    extract_folder(tmp_path, FakeExtractorClient(), fake=True, limit=None)
    truth_path = tmp_path / "a.fake-truth.json"
    truth_path.write_text(json.dumps({**json.loads(truth_path.read_text()), "_reviewed": True, "total": "1.00"}))
    (tmp_path / "a.fake-extracted.json").unlink()  # force a re-extraction
    extract_folder(tmp_path, FakeExtractorClient(), fake=True, limit=None)
    assert json.loads(truth_path.read_text())["total"] == "1.00"
