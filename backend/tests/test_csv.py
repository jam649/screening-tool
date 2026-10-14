import csv
import io
import zipfile
from pathlib import Path

import pytest
from openpyxl import Workbook
from sqlalchemy import func, select

from app.connectors.base import SyncBatch
from app.connectors.csv import CSVConnector, UploadValidationError, preview_upload, read_upload
from app.connectors.sync import apply_sync, sync_csv
from app.csl.service import refresh_csl
from app.models import AuditLog, Party, ScreeningTask, Source

MAPPING = {
    "external_id": "ID",
    "name": "Name",
    "country": "Country",
    "party_type": "Type",
    "role": "Role",
    "address_lines": ["Street", "Unit"],
}


def upload(rows=None):
    rows = rows or [
        [
            "c1",
            "Synthetic Example Ltd",
            "United States",
            "company",
            "customer",
            "1 Main Street",
            "Unit 2",
            "1234",
        ]
    ]
    text = io.StringIO(newline="")
    writer = csv.writer(text)
    writer.writerow(["ID", "Name", "Country", "Type", "Role", "Street", "Unit", "Phone"])
    writer.writerows(rows)
    return text.getvalue().encode()


def saved_source(db, label="Dummy CSV"):
    source = Source(kind="csv", label=label)
    db.add(source)
    db.commit()
    return source


def count(db, model):
    return db.scalar(select(func.count()).select_from(model))


def test_csv_preview_only_first_twenty_and_unicode_bom():
    data = upload(
        [[f"c{i}", f"Example {i}", "US", "company", "vendor", "Street", "", ""] for i in range(25)]
    )
    preview = preview_upload(b"\xef\xbb\xbf" + data, "export.csv")
    assert preview["total_rows"] == 25 and len(preview["rows"]) == 20
    assert preview["headers"][0] == "ID"
    assert "stable" in preview["stable_id_warning"]
    assert preview_upload(data.decode().encode("utf-16"), "export.csv")["total_rows"] == 25
    connector = CSVConnector(upload(), "EXPORT.CSV", MAPPING)
    batch = connector.full_sync()
    party = batch.parties[0]
    assert party.country_iso2 == "US" and party.external_id == "c1"
    assert party.address_lines == ["1 Main Street", "Unit 2"]
    assert party.raw["Phone"] == "1234"
    assert connector.incremental_sync("unused") == batch


def test_xlsx_first_sheet_and_safe_value_conversion():
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Name", "Country", "Code"])
    sheet.append(["Example Ltd", "GBR", 42])
    other = workbook.create_sheet("ignored")
    other.append(["Not", "Imported"])
    buffer = io.BytesIO()
    workbook.save(buffer)
    workbook.close()
    preview = preview_upload(buffer.getvalue(), "export.xlsx")
    assert preview["total_rows"] == 1 and preview["rows"][0]["Code"] == "42"
    assert (
        CSVConnector(buffer.getvalue(), "export.xlsx", {"name": "Name", "country": "Country"})
        .full_sync()
        .parties[0]
        .country_iso2
        == "GB"
    )
    from openpyxl.xml import DEFUSEDXML

    assert DEFUSEDXML


@pytest.mark.parametrize(
    "data,filename",
    [
        (b"", "empty.csv"),
        (b"Name,Name\nA,B\n", "duplicate.csv"),
        (b",Name\n1,A\n", "blank.csv"),
        (b"Name\nExample,extra\n", "extra.csv"),
        (b"Name\n\xff\n", "invalid.csv"),
        (b"Name\nExample\x00\n", "nul.csv"),
        (b"invalid zip", "invalid.xlsx"),
        (b"Name\nExample\n", "executable.exe"),
    ],
)
def test_malformed_uploads_rejected(data, filename):
    with pytest.raises(UploadValidationError):
        read_upload(data, filename)


def test_limits_enforced_before_import(monkeypatch):
    monkeypatch.setattr("app.connectors.csv.MAX_UPLOAD_BYTES", 5)
    with pytest.raises(UploadValidationError, match="10 MiB"):
        read_upload(b"Name\nExample\n", "large.csv")
    monkeypatch.setattr("app.connectors.csv.MAX_UPLOAD_BYTES", 100_000)
    monkeypatch.setattr("app.connectors.csv.MAX_ROWS", 1)
    with pytest.raises(UploadValidationError, match="rows"):
        read_upload(b"Name\nA\nB\n", "rows.csv")
    monkeypatch.setattr("app.connectors.csv.MAX_ROWS", 100)
    monkeypatch.setattr("app.connectors.csv.MAX_CELL_CHARS", 3)
    with pytest.raises(UploadValidationError, match="character"):
        read_upload(b"Name\nExample\n", "cell.csv")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/filler.xml", "x" * 1000)
    monkeypatch.setattr("app.connectors.csv.MAX_EXPANDED_XLSX_BYTES", 10)
    with pytest.raises(UploadValidationError, match="Expanded"):
        read_upload(buffer.getvalue(), "expanded.xlsx")


def test_mapping_validation_and_all_rejected_rows_reported():
    for mapping in [{}, {"name": "Absent"}, {"name": ["Name"]}, {"name": "Name", "secret": "ID"}]:
        with pytest.raises(UploadValidationError):
            CSVConnector(upload(), "export.csv", mapping)
    rows = [
        ["c1", "", "US", "company", "customer", "", "", ""],
        ["c2", "Example", "Not a country", "company", "customer", "", "", ""],
        ["c3", "Example", "US", "planet", "customer", "", "", ""],
        ["c4", "Example", "US", "company", "manager", "", "", ""],
        ["c5", "Example", "US", "company", "vendor", "", "", ""],
        ["c5", "Other example", "US", "company", "customer", "", "", ""],
    ]
    batch = CSVConnector(upload(rows), "export.csv", MAPPING).full_sync()
    assert batch.rejected_count == 5 and [row.row_number for row in batch.rejected] == [
        2,
        3,
        4,
        5,
        7,
    ]
    assert len(batch.parties) == 1
    capped = CSVConnector(upload([rows[0]] * 600), "export.csv", MAPPING).full_sync()
    assert capped.rejected_count == 600 and len(capped.rejected) == 500


def test_sync_reupload_unchanged_phone_edits_changes_and_deletions(client, app):
    sample = Path(__file__).resolve().parents[2] / "fixtures/csl_sample_v1.json"
    with app.state.session_factory() as db:
        refresh_csl(db, file=sample)
        source = saved_source(db)
        first = sync_csv(db, source, upload(), "export.csv", MAPPING)
        assert first.added == 1 and first.applied and len(first.task_ids) == 1
        party = db.scalar(select(Party))
        original_snapshot = party.raw.copy()
        modified_phone = upload(
            [
                [
                    "c1",
                    "Synthetic Example Ltd",
                    "US",
                    "company",
                    "customer",
                    "1 Main Street",
                    "Unit 2",
                    "Changed phone",
                ]
            ]
        )
        same = sync_csv(db, source, modified_phone, "export.csv")
        assert same.unchanged == 1 and same.changed == 0 and not same.task_ids
        assert party.raw == original_snapshot  # no screening-evidence rewrite
        updated = upload(
            [
                ["c1", "New synthetic name", "US", "company", "customer", "", "", ""],
                ["c2", "Another synthetic name", "GB", "individual", "vendor", "", "", ""],
            ]
        )
        result = sync_csv(db, source, updated, "export.csv")
        assert result.changed == 1 and result.added == 1
        assert len(db.get(ScreeningTask, result.task_ids[0]).party_ids) == 2
        removed = sync_csv(
            db,
            source,
            upload([["c2", "Another synthetic name", "GB", "individual", "vendor", "", "", ""]]),
            "export.csv",
        )
        assert removed.deleted == 1 and party.deleted_at is not None
        assert count(db, Party) == 2 and source.record_count == 1
        assert source.last_synced_at is not None and source.last_error is None
        assert source.config["column_mapping"] == MAPPING
        assert count(db, AuditLog) > 0


def test_invalid_full_upload_is_atomic_and_mapping_not_saved(client, app):
    with app.state.session_factory() as db:
        source = saved_source(db)
        sync_csv(db, source, upload(), "export.csv", MAPPING)
        party = db.scalar(select(Party))
        invalid = upload(
            [
                ["c1", "Changed valid name", "US", "company", "customer", "", "", ""],
                ["c2", "Bad country", "Imaginaryland", "company", "vendor", "", "", ""],
            ]
        )
        result = sync_csv(db, source, invalid, "export.csv", {**MAPPING, "address_lines": "Street"})
        assert not result.applied and result.rejected_count == 1
        assert result.payload()["rejected"][0]["row_number"] == 3
        assert party.name == "Synthetic Example Ltd" and party.deleted_at is None
        assert count(db, Party) == 1 and not result.task_ids
        assert source.config["column_mapping"] == MAPPING
        assert source.last_error and source.record_count == 1


def test_no_ids_safe_new_identity_and_reactivation(client, app):
    mapping = {"name": "Name", "country": "Country"}
    with app.state.session_factory() as db:
        source = saved_source(db)
        first = sync_csv(db, source, upload(), "export.csv", mapping)
        original = db.scalar(select(Party))
        assert original.external_id.startswith("generated:") and first.added == 1
        update = upload([["", "Different company", "US", "company", "customer", "", "", ""]])
        changed = sync_csv(db, source, update, "export.csv")
        assert changed.added == 1 and changed.deleted == 1 and original.deleted_at is not None
        restored = sync_csv(db, source, upload(), "export.csv")
        assert restored.changed == 1 and restored.deleted == 1 and original.deleted_at is None
        assert count(db, Party) == 2
        emptied = apply_sync(db, source, SyncBatch([]))
        assert emptied.deleted == 1 and source.record_count == 0
        assert count(db, Party) == 2
        assert all(row.deleted_at is not None for row in db.scalars(select(Party)))


def test_incremental_empty_batch_preserves_parties(client, app):
    with app.state.session_factory() as db:
        source = saved_source(db)
        sync_csv(db, source, upload(), "export.csv", MAPPING)
        result = apply_sync(db, source, SyncBatch([], complete=False, cursor="next"))
        assert result.deleted == 0 and source.cursor == "next" and source.record_count == 1


def test_valid_header_only_export_is_a_full_empty_snapshot(client, app):
    preview = preview_upload(b"Name\n", "headers.csv")
    assert preview["total_rows"] == 0 and preview["rows"] == []
    with app.state.session_factory() as db:
        source = saved_source(db)
        sync_csv(db, source, upload(), "export.csv", MAPPING)
        party_id = db.scalar(select(Party.id))
        result = sync_csv(db, source, b"Name\n", "headers.csv", {"name": "Name"})
        assert result.applied and result.deleted == 1
        assert db.get(Party, party_id).deleted_at is not None
        assert source.record_count == 0
