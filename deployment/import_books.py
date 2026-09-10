#!/usr/bin/env python3
"""Validate the exact good-books CSV, then explicitly upload to an existing index.

Dry-run performs no Azure calls. Live mode uses only the current Azure CLI
identity, never keys, account switching, schema changes, or document deletion.
All fields are source metadata; numeric-looking strings are not normalized.
"""

import argparse
import csv
import datetime
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


ROOT = Path(__file__).resolve().parent.parent
EXPECTED_SHA256 = "a45ed8ece98f12b08a632d4bcf1ed425d76c137614d78136750b9f927317fa53"
SOURCE_COMMIT = "dd3c4cb6bd6514d1a2a833dfd7d8062bed722999"
SOURCE_URL = (
    "https://raw.githubusercontent.com/Azure-Samples/azure-search-sample-data/"
    f"{SOURCE_COMMIT}/good-books/books.csv"
)
FIELDS = (
    "book_id", "goodreads_book_id", "best_book_id", "work_id", "books_count",
    "isbn", "isbn13", "authors", "original_publication_year", "original_title",
    "title", "language_code", "average_rating", "ratings_count",
    "work_ratings_count", "work_text_reviews_count", "ratings_1", "ratings_2",
    "ratings_3", "ratings_4", "ratings_5", "image_url", "small_image_url",
)
API_VERSION = "2025-09-01"
MAX_DOCS = 1000
MAX_BYTES = 16_000_000


class ImportFailure(Exception):
    """Safe-to-print failure, without credentials or raw cloud response bodies."""


class SearchHTTPFailure(ImportFailure):
    def __init__(self, operation, status):
        self.status = status
        super().__init__(f"Search {operation} failed with HTTP {status}; no response body logged.")


def require(condition, message):
    if not condition:
        raise ImportFailure(message)


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def load_documents(path, expected_sha256=EXPECTED_SHA256):
    raw = Path(path).read_bytes()
    sha256 = hashlib.sha256(raw).hexdigest()
    require(sha256 == expected_sha256, "CSV SHA256 mismatch; refusing changed source data.")
    try:
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8"), newline=""), strict=True)
        require(reader.fieldnames == list(FIELDS), "CSV headers must exactly match the source schema.")
        documents = []
        seen = set()
        for row_number, row in enumerate(reader, 2):
            require(None not in row and all(value is not None for value in row.values()),
                    f"CSV record {row_number} has an incorrect field count.")
            book_id = row["book_id"]
            require(re.fullmatch(r"[1-9][0-9]*", book_id) is not None,
                    f"CSV record {row_number} has an invalid book_id.")
            require(book_id not in seen, f"CSV record {row_number} has duplicate book_id {book_id}.")
            require(row["title"].strip(), f"CSV record {row_number} has an empty title.")
            goodreads_id = row["goodreads_book_id"]
            require(re.fullmatch(r"[1-9][0-9]*", goodreads_id) is not None,
                    f"CSV record {row_number} has an invalid goodreads_book_id.")
            seen.add(book_id)
            content = [
                "Book metadata from Azure-Samples/azure-search-sample-data, good-books/books.csv.",
                "No genres, plot summaries, or full text are provided by this dataset.",
                "ISBN values are preserved as supplied; scientific notation may be lossy.",
                f"Source CSV: {SOURCE_URL}",
            ]
            content.extend(f"{field}: {row[field]}" for field in FIELDS if row[field] != "")
            documents.append({
                "id": f"book-{book_id}",
                "title": row["title"],
                "content": "\n".join(content),
                # Constructed from the source identifier, not independently validated.
                "url": f"https://www.goodreads.com/book/show/{goodreads_id}",
            })
    except (UnicodeError, csv.Error) as exc:
        raise ImportFailure("CSV is not valid UTF-8 or well-formed CSV.") from exc
    require(documents, "CSV must contain at least one book.")
    return documents, sha256


def index_payload(documents):
    return json_bytes({"value": [
        {"@search.action": "mergeOrUpload", **document} for document in documents
    ]})


def build_batches(documents, max_docs=MAX_DOCS, max_bytes=MAX_BYTES):
    require(1 <= max_docs <= MAX_DOCS and 1 <= max_bytes <= MAX_BYTES,
            "Batch limits must be within 1000 documents and 16,000,000 bytes.")
    batches = []
    batch = []
    size = len(json_bytes({"value": []}))
    for document in documents:
        document_size = len(json_bytes({"@search.action": "mergeOrUpload", **document}))
        require(document_size + len(json_bytes({"value": []})) <= max_bytes,
                f"Document {document['id']} exceeds the batch byte limit.")
        if batch and (len(batch) == max_docs or size + 1 + document_size > max_bytes):
            batches.append(batch)
            batch = []
            size = len(json_bytes({"value": []}))
        size += document_size + bool(batch)
        batch.append(document)
    if batch:
        batches.append(batch)
    return batches


def validate_target(endpoint, index, subscription, tenant):
    require(re.fullmatch(r"https://[a-z0-9][a-z0-9-]*\.search\.windows\.net/?", endpoint),
            "Endpoint must be a public-cloud HTTPS Search service without credentials, paths, or redirects.")
    require(re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", index),
            "Invalid Search index name.")
    for value in (subscription, tenant):
        require(re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value),
                "Subscription and tenant must be explicit UUIDs.")
    return {
        "endpoint": endpoint.rstrip("/"), "index": index,
        "subscription": subscription.lower(), "tenant": tenant.lower(),
    }


def az_json(*arguments):
    try:
        result = subprocess.run(
            ["az", *arguments, "--output", "json", "--only-show-errors"],
            capture_output=True, check=True, timeout=30,
        )
        return json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise ImportFailure("Azure CLI failed; check authentication privately. No CLI output logged.") from exc


def access_token(target):
    account = az_json("account", "show")
    require(isinstance(account, dict)
            and str(account.get("id", "")).lower() == target["subscription"]
            and str(account.get("tenantId", "")).lower() == target["tenant"]
            and account.get("state") == "Enabled"
            and account.get("environmentName") == "AzureCloud",
            "Active Azure CLI account does not match the enabled target subscription/tenant/public cloud.")
    result = az_json(
        "account", "get-access-token", "--subscription", target["subscription"],
        "--resource", "https://search.azure.com",
    )
    require(isinstance(result, dict), "Malformed Azure CLI token response.")
    require(str(result.get("subscription", "")).lower() == target["subscription"]
            and str(result.get("tenant", "")).lower() == target["tenant"],
            "Azure CLI token subscription/tenant does not match the target.")
    token = result.get("accessToken")
    require(isinstance(token, str) and token and not any(c.isspace() for c in token),
            "Missing or malformed Azure CLI token.")
    return token


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class SearchClient:
    def __init__(self, target):
        self.target = target
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, operation, payload=None):
        require(operation in ("docs/index", "docs/search", "docs/$count")
                or re.fullmatch(r"docs/book-[1-9][0-9]*", operation),
                "Only book document ingestion and verification operations are permitted.")
        require((operation in ("docs/index", "docs/search")) == (payload is not None),
                "Unexpected Search request body.")
        token = access_token(self.target)
        url = (f"{self.target['endpoint']}/indexes/{self.target['index']}/"
               f"{operation}?api-version={API_VERSION}")
        request = urllib.request.Request(
            url, data=payload, method="POST" if payload is not None else "GET",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
                     "Content-Type": "application/json"},
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                require(response.status in ((200, 207) if operation == "docs/index" else (200,)),
                        f"Unexpected Search HTTP status {response.status}.")
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise SearchHTTPFailure(operation, exc.code) from None
        except (OSError, ValueError) as exc:
            raise ImportFailure(f"Search {operation} transport/JSON failure; outcome may be uncertain.") from exc


def check_upload_response(response, documents):
    expected = {document["id"] for document in documents}
    results = response.get("value") if isinstance(response, dict) else None
    require(isinstance(results, list), "Upload returned no per-document results; partial writes possible.")
    seen = set()
    failures = []
    malformed = False
    for result in results:
        if not isinstance(result, dict):
            malformed = True
            continue
        key = result.get("key")
        if not isinstance(key, str) or key not in expected or key in seen:
            malformed = True
            continue
        seen.add(key)
        if result.get("status") is not True or result.get("statusCode") not in (200, 201):
            code = result.get("statusCode")
            failures.append(f"{key} (HTTP {code if isinstance(code, int) else 'unknown'})")
    require(not failures and not malformed and seen == expected,
            f"Upload partially failed or returned incomplete/duplicate results: "
            f"{len(failures)} failed, {len(expected - seen)} missing; "
            + ", ".join(failures[:10])
            + ". Prior writes may exist; rerun idempotently after resolving the error.")


def verify_documents(client, documents, timeout=300):
    deadline = time.monotonic() + timeout
    verified = 0
    for start in range(0, len(documents), 500):
        group = documents[start:start + 500]
        expected = {document["id"]: document for document in group}
        payload = json_bytes({
            "search": "*", "filter": f"search.in(id, '{','.join(expected)}', ',')",
            "select": "id,title,content,url", "top": len(group), "count": True,
        })
        while True:
            response = client.request("docs/search", payload)
            values = response.get("value") if isinstance(response, dict) else None
            require(isinstance(values, list) and all(isinstance(v, dict) for v in values),
                    "Malformed verification response.")
            actual = {value.get("id"): {field: value.get(field) for field in ("id", "title", "content", "url")}
                      for value in values if isinstance(value.get("id"), str)}
            if (actual == expected and len(values) == len(expected)
                    and type(response.get("@odata.count")) is int
                    and response.get("@odata.count") == len(expected)):
                break
            require(time.monotonic() < deadline,
                    f"Timed out verifying book records {start + 1}-{start + len(group)}; "
                    "missing or mismatched indexed fields. Writes may already exist.")
            time.sleep(min(2, max(0, deadline - time.monotonic())))
        verified += len(group)
    samples = []
    for position in sorted({0, len(documents) // 2, len(documents) - 1}):
        expected = documents[position]
        while True:
            try:
                actual = client.request(f"docs/{expected['id']}")
            except SearchHTTPFailure as exc:
                if exc.status != 404:
                    raise
                actual = None
            if (isinstance(actual, dict)
                    and all(actual.get(field) == value for field, value in expected.items())):
                break
            require(time.monotonic() < deadline,
                    f"Exact-key readback mismatch for {expected['id']}; verification timed out.")
            time.sleep(min(2, max(0, deadline - time.monotonic())))
        samples.append({field: expected[field] for field in ("id", "title", "url")})
    while True:
        count = client.request("docs/$count")
        require(type(count) is int and count >= 0,
                "Index count is smaller than the verified dataset or malformed.")
        if count >= len(documents):
            break
        require(time.monotonic() < deadline,
                "Index count is smaller than the verified dataset; verification timed out.")
        time.sleep(min(2, max(0, deadline - time.monotonic())))
    return {"verified_book_count": verified, "index_document_count": count, "examples": samples}


def run(arguments):
    target = validate_target(arguments.endpoint, arguments.index,
                             arguments.subscription, arguments.tenant)
    source = ROOT / arguments.csv
    path = ROOT / arguments.report
    require(path.resolve() != source.resolve(), "Report cannot overwrite the source CSV.")
    documents, sha256 = load_documents(source)
    # Materialize and size-check every batch before obtaining credentials or writing.
    batches = build_batches(documents)
    report = {
        "mode": "dry-run" if arguments.dry_run else "upload",
        "source": SOURCE_URL, "sha256": sha256, "csv_bytes": source.stat().st_size,
        "rows": len(documents), "target": target, "batch_count": len(batches),
        "largest_batch_bytes": max(len(index_payload(batch)) for batch in batches),
        "action": "mergeOrUpload",
    }
    if arguments.dry_run:
        return report
    client = SearchClient(target)
    before = client.request("docs/$count")
    require(type(before) is int and before >= 0, "Malformed pre-upload index count.")
    report["index_document_count_before"] = before
    for number, batch in enumerate(batches, 1):
        response = client.request("docs/index", index_payload(batch))
        check_upload_response(response, batch)
        print(f"Accepted batch {number}/{len(batches)} ({len(batch)} books).", file=sys.stderr)
    report.update(verify_documents(client, documents, arguments.timeout))
    report["uploaded_document_count"] = len(documents)
    report["verified_at_utc"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", default="data/books.csv", help="CSV path, relative to the repository.")
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--index", required=True)
    parser.add_argument("--subscription", required=True)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--dry-run", action="store_true", help="Validate without any cloud calls or writes.")
    parser.add_argument("--timeout", type=int, default=300, help="Eventual-indexing verification wait in seconds.")
    parser.add_argument("--report", default="deployment/.artifacts/books-import.json")
    arguments = parser.parse_args()
    try:
        require(arguments.timeout > 0, "Timeout must be positive.")
        print(json.dumps(run(arguments), indent=2, ensure_ascii=False))
    except (ImportFailure, OSError) as exc:
        print(f"Book import failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
