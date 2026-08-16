"""Generate a sample ZIP of contract documents for local testing.

Usage:
    python scripts/generate_sample_zip.py [output.zip] [num_documents]
"""

import io
import sys
import zipfile


def make_sample_zip(path: str, num_documents: int = 5) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for i in range(num_documents):
            content = f"""RENT AGREEMENT {i + 1}

This agreement is made effective as of 01-Jan-2026 between
Landlord and Tenant.

- Security Deposit: Rs. 200,000
- Monthly Rent: Rs. 25,000
- Maintenance Charges: Rs. 2,500
- Notice Period: 3 months
"""
            zf.writestr(f"contracts/rent_agreement_{i + 1}.txt", content)


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "sample_batch.zip"
    count = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    make_sample_zip(out, count)
    print(f"Wrote {out} with {count} documents")