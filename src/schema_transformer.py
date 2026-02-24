import json
import re
import sys
from collections import Counter


class SchemaTransformer:
    """Transform raw extraction into structured JSON schema """

    SUMMARY_KEYWORDS = {"total", "sum", "subtotal", "grand total", "average",
                        "net total", "net payable", "net commission"}


    @classmethod
    def transform(cls, raw: dict) -> dict:
        return cls(raw)._run()

    def __init__(self, raw: dict):
        self._raw = raw
        self._blocks: list[dict] = []
        self._heading: str | None = None
        self._text_buf: list[str] = []
        self._text_page: int | None = None
        self._pending_table: dict | None = None
        self._last_img_tbl_headers: list | None = None

    @staticmethod
    def _make_block(
        title: str | None,
        description: str | None,
        block_type: str,
        data,
        page=None,
    ) -> dict:
        """Return normalized block: {title, description, type, data}. Optional page for compatibility."""
        block = {
            "title": title or "",
            "description": description or "",
            "type": block_type,
            "data": data,
        }
        if page is not None:
            block["page"] = page
        return block

    def _run(self) -> dict:
        for page_data in self._raw["pages"]:
            self._process_page(page_data)
        self._flush_text()
        self._flush_pending_table()
        return {
            "filename": self._raw["filename"],
            "total_pages": self._raw["total_pages"],
            "blocks": self._blocks,
        }

    def _process_page(self, page_data: dict):
        self._pg = page_data["page_number"]
        self._page_tables = page_data["tables"]
        self._page_images = page_data.get("images", [])
        self._table_idx = 0

        for box in page_data["layout_boxes"]:
            cls = box["boxclass"]
            text = box["text"].strip()

            if cls in ("section-header", "header"):
                self._on_heading(text)
            elif cls == "table":
                self._on_table(box)
            elif cls == "picture":
                self._on_picture(box)
            elif cls in ("text", "list-item") and text:
                self._on_text(text)

    def _on_heading(self, text: str):
        self._flush_text()
        self._flush_pending_table()
        self._heading = text.split("\n")[-1].strip() if "\n" in text else text

    def _on_text(self, text: str):
        if not self._text_buf:
            self._text_page = self._pg
        self._text_buf.append(text)

    def _on_table(self, box: dict):
        self._last_img_tbl_headers = None

        headers, all_rows, tab = self._resolve_table_data(box)
        if tab is None:
            return

        title, description = self._resolve_title_description()

        if self._is_key_value_table(all_rows):
            self._flush_pending_table()
            self._blocks.append(self._make_block(
                title, description, "table",
                {"key_value": self._extract_key_values(all_rows)},
                page=self._pg,
            ))
            return

        data_rows = [r for r in all_rows if r != headers]

        if self._try_merge_table(headers, data_rows, all_rows, tab):
            return

        self._flush_pending_table()
        summary, row_values = None, []
        for row in data_rows:
            if self._is_summary_row(row):
                summary = list(row)
            else:
                row_values.append(list(row))

        self._pending_table = {
            "page": [self._pg],
            "title": title,
            "description": description,
            "headers": headers,
            "rows": row_values,
            "summary_row": summary,
            "_raw_headers": headers,
            "_trailing": tab.get("trailing_text"),
        }

    def _on_picture(self, box: dict):
        self._flush_text()
        self._flush_pending_table()
        pg = self._pg

        ocr_text = self._match_ocr_text(box)
        img_headers = self._detect_image_table(ocr_text)

        if img_headers:
            rows, summary = self._parse_image_table(ocr_text, img_headers)
            description = "\n".join(self._text_buf) if self._text_buf else ""
            self._text_buf, self._text_page = [], None
            data = {"headers": img_headers, "rows": rows, "summary_row": summary}
            self._blocks.append(self._make_block(
                self._heading, description, "image_table", data, page=pg
            ))
            self._last_img_tbl_headers = img_headers

        elif self._last_img_tbl_headers and ocr_text:
            rows, summary = self._parse_image_table(
                ocr_text, self._last_img_tbl_headers, skip_header=True)
            if rows:
                data = {
                    "headers": self._last_img_tbl_headers,
                    "rows": rows,
                    "summary_row": summary,
                }
                self._blocks.append(self._make_block(
                    self._heading, "", "image_table", data, page=pg
                ))
            else:
                self._blocks.append(self._make_block(
                    self._heading, "", "image_text", ocr_text, page=pg
                ))
        else:
            self._blocks.append(self._make_block(
                self._heading, "", "image_text", ocr_text, page=pg
            ))

    def _flush_text(self):
        if self._text_buf:
            content = "\n".join(self._text_buf)
            self._blocks.append(self._make_block(
                self._heading, "", "text_block", content, page=self._text_page
            ))
            self._text_buf, self._text_page = [], None

    def _flush_pending_table(self):
        pt = self._pending_table
        if not pt:
            return
        if not pt["summary_row"] and pt.get("_trailing"):
            totals = self._parse_trailing_totals(pt["_trailing"])
            if totals:
                pt["summary_row"] = [totals["label"]] + totals["values"]
        page = pt["page"]
        if len(page) == 1:
            page = page[0]
        data = {
            "headers": pt["headers"],
            "rows": pt["rows"],
            "summary_row": pt["summary_row"],
        }
        self._blocks.append(self._make_block(
            pt["title"], pt["description"], "table", data, page=page
        ))
        self._pending_table = None

    def _resolve_table_data(self, box: dict):
        """Return (headers, all_rows, tab_dict) or (None, None, None)."""
        if self._table_idx < len(self._page_tables):
            tab = self._page_tables[self._table_idx]
            self._table_idx += 1
            headers = [self._clean_cell(h) for h in tab["header_names"]]
            all_rows = [[self._clean_cell(c) for c in row] for row in tab["cells"]]
            return headers, all_rows, tab

        if box.get("layout_table"):
            lt = box["layout_table"]
            all_rows = [[self._clean_cell(c) for c in row] for row in lt["cells"]]
            headers = all_rows[0] if all_rows else []
            tab = {
                "col_count": lt["col_count"],
                "row_count": lt["row_count"],
                "header_names": headers,
                "cells": lt["cells"],
            }
            return headers, all_rows, tab

        return None, None, None

    def _resolve_title_description(self):
        title, description = self._heading, None
        if self._text_buf:
            if not self._heading and len(self._text_buf) == 1 and len(self._text_buf[0]) < 80:
                title = self._text_buf[0]
            else:
                description = "\n".join(self._text_buf)
            self._text_buf, self._text_page = [], None
        return title, description

    def _try_merge_table(self, headers, data_rows, all_rows, tab) -> bool:
        """Try merging into pending_table. Returns True if merged."""
        pt = self._pending_table
        if not pt:
            return False

        prev_headers = pt["_raw_headers"]
        last_pg = pt["page"][-1]
        can_merge = False
        use_pending_headers = False

        if self._headers_match(prev_headers, headers):
            can_merge = True
        elif len(prev_headers) == len(headers) and self._pg <= last_pg + 1:
            can_merge = True
            use_pending_headers = True

        if not can_merge:
            return False

        rows_to_add = all_rows if use_pending_headers else data_rows
        for row in rows_to_add:
            if self._is_summary_row(row):
                pt["summary_row"] = list(row)
            else:
                pt["rows"].append(list(row))
        if self._pg not in pt["page"]:
            pt["page"].append(self._pg)
        trailing = tab.get("trailing_text")
        if trailing:
            pt["_trailing"] = trailing
        return True

    def _match_ocr_text(self, box: dict) -> str:
        """Find the OCR text for an image whose bbox overlaps this layout box."""
        box_rect = box["bbox"]
        for img in self._page_images:
            img_rect = img["bbox"]
            if (abs(img_rect[0] - box_rect[0]) < 30
                    and abs(img_rect[1] - box_rect[1]) < 30):
                return img.get("ocr_text", "")
        return ""

    @staticmethod
    def _fix_dollar(digits: str, negative: bool) -> str:
        if len(digits) <= 2:
            formatted = f"0.{digits.zfill(2)}"
        else:
            formatted = f"{int(digits[:-2]):,}.{digits[-2:]}"
        return f"(${formatted})" if negative else f"${formatted}"

    @staticmethod
    def _clean_cell(val):
        if val is None:
            return None
        stripped = re.sub(r'\n[, .]*\s*$', '', val).strip()

        m = re.match(r'^(\()\$\s*(\d+)\s*(\))$', stripped)
        if m:
            return SchemaTransformer._fix_dollar(m.group(2), negative=True)
        m = re.match(r'^\$\s*(-?)(\d+)$', stripped)
        if m:
            return SchemaTransformer._fix_dollar(m.group(2), negative=bool(m.group(1)))

        if re.match(r'^\$\s*-?\s*0\s*\.?\s*00$', stripped):
            return "$0.00"

        m = re.match(r'^(\d+)%$', stripped)
        if m:
            digits = m.group(1)
            if '\n' in val and len(digits) >= 2:
                return digits[:-1] + '.' + digits[-1] + '%'
            return digits + '%'

        stripped = re.sub(r'\s+', ' ', stripped)
        return stripped if stripped else None


    @classmethod
    def _is_summary_row(cls, row) -> bool:
        for cell in row:
            if not cell:
                continue
            lower = cell.strip().lower()
            if len(lower) > 60:
                continue
            if any(re.search(r'\b' + re.escape(kw) + r'\b', lower)
                   for kw in cls.SUMMARY_KEYWORDS):
                return True
        return False

    @staticmethod
    def _headers_match(h1, h2) -> bool:
        return len(h1) == len(h2) and h1 == h2

    @staticmethod
    def _is_key_value_table(cells) -> bool:
        if not cells or len(cells) < 2 or len(cells[0]) < 2:
            return False
        ncols = len(cells[0])
        for key_col in range(0, ncols, 2):
            colon_count = sum(
                1 for row in cells
                if row[key_col] and row[key_col].rstrip().endswith(":")
            )
            if colon_count < len(cells) * 0.5:
                return False
        return True

    @staticmethod
    def _extract_key_values(cells) -> dict:
        ncols = len(cells[0])
        kv = {}
        for row in cells:
            for key_col in range(0, ncols - 1, 2):
                key, val = row[key_col], row[key_col + 1]
                if key:
                    kv[key.rstrip(":").strip()] = val
        return kv

    @staticmethod
    def _parse_trailing_totals(trailing_text):
        if not trailing_text:
            return None
        lines = trailing_text.strip().split("\n")
        total_idx = None
        for i, line in enumerate(lines):
            if any(kw in line.lower() for kw in ("total", "totals")):
                total_idx = i
                break
        if total_idx is None:
            return None
        label = lines[total_idx].strip()
        values = [ln.strip() for ln in lines[total_idx + 1:] if ln.strip()]
        return {"label": label, "values": values} if values else None

    @staticmethod
    def _detect_image_table(ocr_text: str) -> list | None:
        if not ocr_text:
            return None
        lines = [ln.strip() for ln in ocr_text.strip().split("\n") if ln.strip()]
        if len(lines) < 5:
            return None

        line_counts = Counter(lines)
        start = 1 if len(lines[0]) > 30 else 0

        headers = []
        for i in range(start, min(start + 6, len(lines))):
            line = lines[i]
            if len(line) < 30 and line_counts[line] == 1:
                headers.append(line)
            else:
                break

        if len(headers) < 2:
            return None
        remaining = len(lines) - start - len(headers)
        return headers if remaining >= len(headers) * 2 else None

    @classmethod
    def _parse_image_table(cls, ocr_text, headers, skip_header=False):
        lines = [ln.strip() for ln in ocr_text.strip().split("\n") if ln.strip()]
        if skip_header:
            data_lines = lines
        else:
            header_last = lines.index(headers[-1])
            data_lines = lines[header_last + 1:]
        ncols = len(headers)

        def is_row_start(line):
            clean = line.replace(" ", "")
            return len(line) < 20 and clean.isalpha() and "-" not in line

        row_groups, current = [], []
        for line in data_lines:
            if is_row_start(line) and current:
                row_groups.append(current)
                current = [line]
            else:
                current.append(line)
        if current:
            row_groups.append(current)

        rows, summary = [], None
        for group in row_groups:
            if len(group) >= ncols:
                padded = group[:ncols - 1] + [" ".join(group[ncols - 1:])]
            else:
                padded = group + [None] * (ncols - len(group))

            if cls._is_summary_row(padded):
                summary = list(padded)
            else:
                rows.append(list(padded))

        return rows, summary


if __name__ == "__main__":
    raw_path = sys.argv[1] if len(sys.argv) > 1 else "output/raw/1.json"
    name = raw_path.split("/")[-1].replace(".json", "")

    with open(raw_path) as f:
        raw = json.load(f)

    result = SchemaTransformer.transform(raw)

    out_path = f"output/{name}.json"
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)