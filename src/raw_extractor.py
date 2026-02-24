import json
import sys
import pymupdf


class RawExtractor:
    """Extract raw tables, layout boxes, and OCR from a PDF into a JSON dictionary. """

    MIN_IMAGE_SIZE = 50

    def __init__(self, pdf_path: str, ocr_dpi: int = 600):
        self.pdf_path = pdf_path
        self.ocr_dpi = ocr_dpi

    #extract_tablesmust run BEFORE pymupdf.layout import
    def _extract_tables(self) -> dict[int, list[dict]]:
        """Run find_tables() on every page. Returns {page_number: [table_dict, ...]}."""
        doc = pymupdf.open(self.pdf_path)
        tables_by_page = {}

        for page_num in range(doc.page_count):
            page = doc.load_page(page_num)
            page_tables = self._tables_from_page(page)
            self._attach_trailing_totals(page, page_tables)
            tables_by_page[page_num + 1] = page_tables

        doc.close()
        return tables_by_page

    def _tables_from_page(self, page) -> list[dict]:
        tables = []
        for tab in page.find_tables().tables:
            try:
                bbox = list(tab.bbox)
            except ValueError:
                continue
            tables.append({
                "bbox": bbox,
                "col_count": tab.col_count,
                "row_count": tab.row_count,
                "header_external": tab.header.external,
                "header_names": tab.header.names,
                "cells": tab.extract(),
            })
        return tables

    def _attach_trailing_totals(self, page, page_tables: list[dict]):
        """Look for "total(s)" text near the bottom edge of each table."""
        text_blocks = page.get_text("blocks")
        for table_info in page_tables:
            bottom = table_info["bbox"][3]
            for block in text_blocks:
                if block[-1] != 0:
                    continue
                by0, btext = block[1], block[4].strip()
                if bottom - 30 <= by0 <= bottom + 30:
                    if any(kw in btext.lower() for kw in ("total", "totals")):
                        table_info["trailing_text"] = btext
                        break

    def _extract_layout_boxes(self, page_layout) -> list[dict]:
        """Convert pymupdf4llm boxes into serialisable dicts."""
        boxes = []
        for box in page_layout.boxes:
            entry = {
                "boxclass": box.boxclass,
                "bbox": [box.x0, box.y0, box.x1, box.y1],
                "text": self._box_text(box),
            }
            layout_table = self._layout_table_fallback(box)
            if layout_table:
                entry["layout_table"] = layout_table
            boxes.append(entry)
        return boxes

    @staticmethod
    def _box_text(box) -> str:
        if not (hasattr(box, "textlines") and box.textlines):
            return ""
        return "\n".join(
            span["text"]
            for line in box.textlines
            for span in line.get("spans", [])
            if span.get("text", "").strip()
        )

    @staticmethod
    def _layout_table_fallback(box) -> dict | None:
        """Capture table data from pymupdf4llm when find_tables() missed it."""
        if box.boxclass != "table":
            return None
        if not (hasattr(box, "table") and isinstance(box.table, dict)):
            return None
        tbl = box.table
        if not tbl.get("extract"):
            return None
        return {
            "col_count": tbl.get("col_count", 0),
            "row_count": tbl.get("row_count", 0),
            "cells": tbl["extract"],
        }


    def _extract_images(self, page) -> list[dict]:
        """Find raster images on a page and OCR each one."""
        images = []
        blocks = page.get_text("dict", flags=pymupdf.TEXT_PRESERVE_IMAGES)["blocks"]
        for block in blocks:
            if block["type"] != 1:
                continue
            bbox = pymupdf.Rect(block["bbox"])
            if bbox.width < self.MIN_IMAGE_SIZE or bbox.height < self.MIN_IMAGE_SIZE:
                continue
            images.append({
                "bbox": list(bbox),
                "ocr_text": self._ocr_image(page, bbox),
            })
        return images

    def _ocr_image(self, page, bbox) -> str:
        pix = page.get_pixmap(dpi=self.ocr_dpi, clip=bbox)
        ocrpdf = pymupdf.open("pdf", pix.pdfocr_tobytes())
        text = ocrpdf[0].get_text().strip()
        ocrpdf.close()
        return text


    @classmethod
    def extract(cls, pdf_path: str, ocr_dpi: int = 600) -> dict:
        return cls(pdf_path, ocr_dpi)._run()

    def _run(self) -> dict:
        tables_by_page = self._extract_tables()

        import pymupdf.layout  # noqa: F401 — must be AFTER _extract_tables
        import pymupdf4llm

        doc = pymupdf.open(self.pdf_path)
        parsed = pymupdf4llm.parse_document(doc, use_ocr=False, force_text=True)

        result = {
            "filename": self.pdf_path,
            "total_pages": doc.page_count,
            "pages": [],
        }

        for page_layout in parsed.pages:
            page_num = page_layout.page_number
            page = doc.load_page(page_num - 1)
            result["pages"].append({
                "page_number": page_num,
                "tables": tables_by_page.get(page_num, []),
                "layout_boxes": self._extract_layout_boxes(page_layout),
                "images": self._extract_images(page),
            })

        doc.close()
        return result


if __name__ == "__main__":
    pdf_path = sys.argv[1]
    name = pdf_path.split("/")[-1].replace(".pdf", "")

    print(f"Extracting {pdf_path}...")
    raw = RawExtractor.extract(pdf_path)
    out_path = f"output/raw/{name}.json"
    with open(out_path, "w") as f:
        json.dump(raw, f, indent=2, ensure_ascii=False)
    print(f"Done -> {out_path} ({len(json.dumps(raw))} chars)")