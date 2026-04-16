"""CSV export service for structured research data.

Produces a ZIP bundle containing:
- data.csv: one row per extracted item, dimension values repeated
- sources.csv: one row per source with provenance metadata
"""

import csv
import io
import zipfile
from typing import Any, Dict, List, Optional

from loguru import logger

from .base import BaseExporter, ExportOptions, ExportResult
from .registry import ExporterRegistry


def _flatten_items(structured_data: Dict[str, Any]) -> tuple:
    """Extract column headers and rows from structured data.

    Returns:
        (dimension_names, field_names, rows) where each row is a dict
        with dimension values + field values + metadata.
    """
    cells = structured_data.get("cells", [])
    schema = structured_data.get("schema", {})

    # Collect dimension names from cells
    dimension_names: List[str] = []
    for cell in cells:
        for key in cell.get("dimension_values", {}):
            if key not in dimension_names:
                dimension_names.append(key)

    # Collect field names from schema, falling back to item keys
    field_names: List[str] = []
    for field_def in schema.get("fields", []):
        name = field_def.get("name", "")
        if name:
            field_names.append(name)

    # If no schema fields, infer from first non-empty cell's items
    if not field_names:
        for cell in cells:
            items = cell.get("items", [])
            if items:
                for key in items[0]:
                    if key not in (
                        "source_ids",
                        "source_count",
                        "confidence",
                        "item_id",
                        "conflicts",
                        "appended_from",
                    ):
                        field_names.append(key)
                break

    # For deep nesting, prefix dimension names with level
    has_levels = any(cell.get("level", 1) > 1 for cell in cells)
    if has_levels:
        prefixed_dims = [f"L{i + 1}_{d}" for i, d in enumerate(dimension_names)]
    else:
        prefixed_dims = list(dimension_names)

    meta_columns = [
        "item_id",
        "confidence",
        "source_count",
        "source_ids",
        "researched_at",
    ]

    # Check if any items have raw enum fields
    raw_columns: List[str] = []
    for cell in cells:
        for item in cell.get("items", []):
            for key in item:
                if key.startswith("_") and key.endswith("_raw") and key not in raw_columns:
                    raw_columns.append(key)

    headers = prefixed_dims + field_names + raw_columns + meta_columns
    rows: List[Dict[str, str]] = []

    for cell in cells:
        dim_values = cell.get("dimension_values", {})
        for item in cell.get("items", []):
            row: Dict[str, str] = {}

            # Dimension values
            for orig, col in zip(dimension_names, prefixed_dims):
                row[col] = str(dim_values.get(orig, ""))

            # Field values
            for fname in field_names:
                row[fname] = str(item.get(fname, ""))

            # Raw enum values
            for rcol in raw_columns:
                row[rcol] = str(item.get(rcol, ""))

            # Metadata
            row["item_id"] = str(item.get("item_id", ""))
            row["confidence"] = str(item.get("confidence", ""))
            row["source_count"] = str(item.get("source_count", ""))
            row["source_ids"] = ";".join(item.get("source_ids", []))
            row["researched_at"] = str(cell.get("researched_at", ""))

            rows.append(row)

    return headers, rows


def _build_sources_csv(structured_data: Dict[str, Any]) -> tuple:
    """Build headers and rows for sources.csv."""
    sources = structured_data.get("sources", [])
    headers = [
        "id",
        "url",
        "title",
        "snippet",
        "content_date",
        "discovered_at",
        "source_type",
        "engine",
    ]
    rows: List[Dict[str, str]] = []
    for src in sources:
        rows.append(
            {
                "id": str(src.get("id", "")),
                "url": str(src.get("url", "")),
                "title": str(src.get("title", "")),
                "snippet": str(src.get("snippet", "")),
                "content_date": str(src.get("content_date", "")),
                "discovered_at": str(src.get("discovered_at", "")),
                "source_type": str(src.get("source_type", "")),
                "engine": str(src.get("engine", "")),
            }
        )
    return headers, rows


def _write_csv_bytes(headers: List[str], rows: List[Dict[str, str]]) -> bytes:
    """Write CSV to bytes with UTF-8 BOM for Excel compatibility."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=headers, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    # UTF-8 BOM so Excel auto-detects encoding
    return b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8")


@ExporterRegistry.register
class CSVExporter(BaseExporter):
    """Export structured research data as a ZIP bundle (data.csv + sources.csv)."""

    @property
    def format_name(self) -> str:
        return "csv"

    @property
    def file_extension(self) -> str:
        return ".zip"

    @property
    def mimetype(self) -> str:
        return "application/zip"

    def export(
        self,
        markdown_content: str,
        options: Optional[ExportOptions] = None,
    ) -> ExportResult:
        """Export structured data as a CSV ZIP bundle.

        Requires structured_data in options.custom_options.
        Returns a ZIP containing data.csv and sources.csv.

        Raises:
            ValueError: If no structured data is provided.
        """
        options = options or ExportOptions()
        structured_data = (options.custom_options or {}).get("structured_data")

        if not structured_data:
            raise ValueError(
                "CSV export requires structured research data. "
                "Use the structured research mode to generate exportable data."
            )

        try:
            # Build data CSV
            data_headers, data_rows = _flatten_items(structured_data)
            data_bytes = _write_csv_bytes(data_headers, data_rows)

            # Build sources CSV
            src_headers, src_rows = _build_sources_csv(structured_data)
            src_bytes = _write_csv_bytes(src_headers, src_rows)

            # Bundle into ZIP
            zip_buf = io.BytesIO()
            with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("data.csv", data_bytes)
                zf.writestr("sources.csv", src_bytes)

            zip_bytes = zip_buf.getvalue()
            filename = self._generate_safe_filename(options.title)

            logger.info(
                f"Generated CSV ZIP bundle: {len(data_rows)} data rows, "
                f"{len(src_rows)} sources, {len(zip_bytes)} bytes"
            )

            return ExportResult(
                content=zip_bytes,
                filename=filename,
                mimetype=self.mimetype,
            )

        except ValueError:
            raise
        except Exception:
            logger.exception("Error generating CSV export")
            raise

