# pdf-parser

Extract metadata, text, tables, and images from PDF files into structured JSON.

## Setup

1. **Install Tesseract** (required for OCR on images):

   ```bash
   brew install tesseract
   ```

2. **Install the project**:

   ```bash
   pip install -e .
   ```

## Usage

```python
from src.raw_extractor import RawExtractor
from src.schema_transformer import SchemaTransformer
from src.data_extractor import Document

raw_data = RawExtractor.extract("pdfs/yourfile.pdf")
structured_data = SchemaTransformer.transform(raw_data)
doc = Document(structured_data)

for block_id, title, entity_type in doc.blocks():
    data = doc.get_data(block_id)
```

Or run `extraction_test.ipynb` for a quick demo.
