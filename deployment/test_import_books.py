"""Offline regression coverage for the pinned books importer."""

import argparse
import copy
import csv
import datetime
import hashlib
import io
import json
from contextlib import ExitStack
from pathlib import Path
import socket
import subprocess
import urllib.error
import urllib.request
from unittest.mock import Mock, call, patch

import pytest

import import_books


PINNED_SHA256 = "a45ed8ece98f12b08a632d4bcf1ed425d76c137614d78136750b9f927317fa53"
PINNED_SOURCE = (
    "https://raw.githubusercontent.com/Azure-Samples/azure-search-sample-data/"
    "dd3c4cb6bd6514d1a2a833dfd7d8062bed722999/good-books/books.csv"
)
SOURCE_FIELDS = (
    "book_id", "goodreads_book_id", "best_book_id", "work_id", "books_count",
    "isbn", "isbn13", "authors", "original_publication_year", "original_title",
    "title", "language_code", "average_rating", "ratings_count",
    "work_ratings_count", "work_text_reviews_count", "ratings_1", "ratings_2",
    "ratings_3", "ratings_4", "ratings_5", "image_url", "small_image_url",
)
CONTENT_PREFIX = "\n".join((
    "Book metadata from Azure-Samples/azure-search-sample-data, good-books/books.csv.",
    "No genres, plot summaries, or full text are provided by this dataset.",
    "ISBN values are preserved as supplied; scientific notation may be lossy.",
    f"Source CSV: {PINNED_SOURCE}",
))
SUBSCRIPTION = "abcdef01-2345-6789-abcd-0123456789ab"
TENANT = "fedcba98-7654-3210-dcba-9876543210fe"
TARGET = {
    "endpoint": "https://example-search.search.windows.net",
    "index": "books",
    "subscription": SUBSCRIPTION,
    "tenant": TENANT,
}
SECRET = "private-credential-do-not-log"
PATH_MKDIR = Path.mkdir
PATH_WRITE_TEXT = Path.write_text


@pytest.fixture(autouse=True)
def prevent_external_side_effects():
    """Any unmocked external operation fails instead of touching Azure or disk."""
    with ExitStack() as stack:
        for owner, name in (
            (subprocess, "run"),
            (subprocess, "Popen"),
            (socket, "create_connection"),
            (socket.socket, "connect"),
            (socket.socket, "connect_ex"),
            (urllib.request, "urlopen"),
            (urllib.request.OpenerDirector, "open"),
            (Path, "mkdir"),
            (Path, "write_text"),
            (Path, "write_bytes"),
        ):
            stack.enter_context(patch.object(
                owner, name, side_effect=AssertionError(f"Unmocked external operation: {name}"),
            ))
        stack.enter_context(patch.object(
            import_books.time, "sleep", side_effect=AssertionError("Unmocked verification sleep"),
        ))
        yield


def source_row(**changes):
    row = dict.fromkeys(SOURCE_FIELDS, "")
    row.update(book_id="1", goodreads_book_id="42", title="A book")
    row.update(changes)
    return row


def csv_bytes(rows, fields=SOURCE_FIELDS):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(fields)
    for row in rows:
        writer.writerow([row[field] for field in fields])
    return stream.getvalue().encode("utf-8")


def load_bytes(raw):
    with patch.object(Path, "read_bytes", return_value=raw) as read:
        result = import_books.load_documents(
            Path("data/in-memory-books.csv"), expected_sha256=hashlib.sha256(raw).hexdigest(),
        )
    read.assert_called_once_with()
    return result


def mapped_document(row):
    return {
        "id": f"book-{row['book_id']}",
        "title": row["title"],
        "content": CONTENT_PREFIX + "\n" + "\n".join(
            f"{field}: {row[field]}" for field in SOURCE_FIELDS if row[field] != ""
        ),
        "url": f"https://www.goodreads.com/book/show/{row['goodreads_book_id']}",
    }


def documents(count=3):
    return [
        {"id": f"book-{number}", "title": f"Title {number}",
         "content": f"Metadata {number}", "url": f"https://www.goodreads.com/book/show/{number}"}
        for number in range(1, count + 1)
    ]


def encoded_payload(docs):
    return json.dumps(
        {"value": [{"@search.action": "mergeOrUpload", **doc} for doc in docs]},
        ensure_ascii=False, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


def upload_response(docs):
    return {"value": [
        {"key": doc["id"], "status": True, "statusCode": 201} for doc in docs
    ]}


def search_response(docs):
    return {"value": copy.deepcopy(docs), "@odata.count": len(docs)}


def account_response(**changes):
    return {
        "id": SUBSCRIPTION, "tenantId": TENANT, "state": "Enabled",
        "environmentName": "AzureCloud", **changes,
    }


def arguments(**changes):
    return argparse.Namespace(**{
        "endpoint": TARGET["endpoint"], "index": TARGET["index"],
        "subscription": SUBSCRIPTION, "tenant": TENANT,
        "csv": "data/books.csv", "report": "deployment/.artifacts/books-import.json",
        "dry_run": False, "timeout": 30, **changes,
    })


@pytest.fixture(scope="module")
def real_csv():
    raw = (import_books.ROOT / "data/books.csv").read_bytes()
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8"), newline=""), strict=True))
    return raw, rows


def test_pinned_real_csv_maps_every_field_of_all_10000_records(real_csv):
    raw, rows = real_csv
    assert hashlib.sha256(raw).hexdigest() == PINNED_SHA256
    assert import_books.EXPECTED_SHA256 == PINNED_SHA256
    assert import_books.SOURCE_URL == PINNED_SOURCE
    assert import_books.FIELDS == SOURCE_FIELDS
    assert len(raw) == 3_182_802
    actual, digest = import_books.load_documents(import_books.ROOT / "data/books.csv")
    assert digest == PINNED_SHA256
    assert len(actual) == len(rows) == 10_000
    assert actual == [mapped_document(row) for row in rows]
    assert {doc["id"] for doc in actual} == {f"book-{number}" for number in range(1, 10_001)}
    assert actual[0]["title"] == "The Hunger Games (The Hunger Games, #1)"
    assert actual[5000]["title"] == "High School Debut, Vol. 01 (High School Debut, #1)"
    assert actual[-1]["title"] == "The First World War"
    assert "isbn13: 9.78E+12\n" in actual[0]["content"]
    assert "original_title: 高校デビュー 1\n" in actual[5000]["content"]
    assert "language_code:" not in actual[-1]["content"]


def test_metadata_preserves_all_raw_strings_and_omits_only_empty_fields():
    row = source_row(
        title='  Cien años, "世界"\nSecond line  ', authors=" García Márquez ",
        isbn="0000439023483", isbn13="9.78E+12", original_publication_year="-350.0",
        average_rating="4.00", ratings_count="00012", language_code=" ",
        original_title="", image_url="https://example.invalid/é?size=1",
    )
    actual, digest = load_bytes(csv_bytes([row]))
    assert actual == [mapped_document(row)]
    assert digest == hashlib.sha256(csv_bytes([row])).hexdigest()
    assert "isbn: 0000439023483\nisbn13: 9.78E+12\n" in actual[0]["content"]
    assert "language_code:  \n" in actual[0]["content"]
    assert "original_title:" not in actual[0]["content"]
    assert "small_image_url:" not in actual[0]["content"]


@pytest.mark.parametrize("field", ["book_id", "goodreads_book_id"])
@pytest.mark.parametrize("value", ["", "0", "-1", "+1", "01", "1.0", "1e2", " 1", "1 ", "١", "1/2", "1\n"])
def test_invalid_source_ids_rejected(field, value):
    with pytest.raises(import_books.ImportFailure, match=f"invalid {field}"):
        load_bytes(csv_bytes([source_row(**{field: value})]))


@pytest.mark.parametrize("title", ["", " ", "\t\r\n"])
def test_empty_or_whitespace_titles_rejected(title):
    with pytest.raises(import_books.ImportFailure, match="empty title"):
        load_bytes(csv_bytes([source_row(title=title)]))


def test_duplicate_book_ids_rejected_even_with_different_metadata():
    with pytest.raises(import_books.ImportFailure, match="record 3.*duplicate book_id 1"):
        load_bytes(csv_bytes([source_row(), source_row(title="Different", goodreads_book_id="99")]))


def test_shared_goodreads_id_is_not_a_duplicate_document_key():
    rows = [source_row(), source_row(book_id="2")]
    actual, _ = load_bytes(csv_bytes(rows))
    assert actual == [mapped_document(row) for row in rows]
    assert actual[0]["url"] == actual[1]["url"]
    assert actual[0]["id"] != actual[1]["id"]


@pytest.mark.parametrize("fields", [
    SOURCE_FIELDS[:-1],
    (SOURCE_FIELDS[1], SOURCE_FIELDS[0], *SOURCE_FIELDS[2:]),
    (*SOURCE_FIELDS[:-1], SOURCE_FIELDS[0]),
    (*SOURCE_FIELDS, SOURCE_FIELDS[-1]),
    ("BOOK_ID", *SOURCE_FIELDS[1:]),
])
def test_missing_reordered_duplicate_or_renamed_headers_rejected(fields):
    row = {**source_row(), "BOOK_ID": "1"}
    with pytest.raises(import_books.ImportFailure, match="headers must exactly match"):
        load_bytes(csv_bytes([row], fields=fields))


@pytest.mark.parametrize("raw", [b"", b"\xef\xbb\xbf" + csv_bytes([source_row()])])
def test_missing_header_and_utf8_bom_rejected(raw):
    with pytest.raises(import_books.ImportFailure, match="headers must exactly match"):
        load_bytes(raw)


def test_header_only_csv_rejected():
    with pytest.raises(import_books.ImportFailure, match="at least one book"):
        load_bytes(csv_bytes([]))


@pytest.mark.parametrize("record", [
    "1,42\n",
    ",".join(["1", "42"] + ["x"] * 22) + "\n",
])
def test_malformed_field_count_rejected(record):
    with pytest.raises(import_books.ImportFailure, match="incorrect field count"):
        load_bytes(csv_bytes([]) + record.encode("utf-8"))


@pytest.mark.parametrize("record", [b'1,42,"unterminated', b'1,42,"closed"x\n', b"\xff"])
def test_invalid_csv_quoting_and_utf8_rejected(record):
    with pytest.raises(import_books.ImportFailure, match="not valid UTF-8 or well-formed CSV"):
        load_bytes(csv_bytes([]) + record)


def test_hash_mismatch_is_rejected_before_decoding_or_parsing():
    with patch.object(Path, "read_bytes", return_value=b"\xff"), patch.object(
        import_books.csv, "DictReader", side_effect=AssertionError("Must not parse untrusted bytes"),
    ):
        with pytest.raises(import_books.ImportFailure, match="SHA256 mismatch"):
            import_books.load_documents(Path("data/books.csv"))


def test_default_hash_is_not_silently_replaced_with_hash_of_input():
    with patch.object(Path, "read_bytes", return_value=csv_bytes([source_row()])):
        with pytest.raises(import_books.ImportFailure, match="SHA256 mismatch"):
            import_books.load_documents(Path("data/books.csv"))


def test_payload_is_exact_compact_utf8_merge_or_upload_without_mutation():
    docs = documents(1)
    docs[0]["title"] = 'é,世界 "quoted"\n📚'
    original = copy.deepcopy(docs)
    actual = import_books.index_payload(docs)
    assert isinstance(actual, bytes)
    assert actual == encoded_payload(docs)
    assert b"\\u" not in actual
    assert json.loads(actual) == {"value": [{"@search.action": "mergeOrUpload", **docs[0]}]}
    assert docs == original
    assert import_books.index_payload([]) == b'{"value":[]}'


def test_batch_defaults_match_azure_limits():
    assert import_books.MAX_DOCS == 1000
    assert import_books.MAX_BYTES == 16_000_000


@pytest.mark.parametrize("count,expected_sizes", [
    (0, []), (1, [1]), (999, [999]), (1000, [1000]),
    (1001, [1000, 1]), (2001, [1000, 1000, 1]),
])
def test_batch_document_count_boundaries(count, expected_sizes):
    docs = documents(count)
    original = copy.deepcopy(docs)
    batches = import_books.build_batches(docs)
    assert [len(batch) for batch in batches] == expected_sizes
    assert [doc for batch in batches for doc in batch] == docs
    assert docs == original
    assert all(len(encoded_payload(batch)) <= 16_000_000 for batch in batches)


@pytest.mark.parametrize("text", ["ascii", "é", "世界", "📚", 'é, "quoted"\n世界'])
@pytest.mark.parametrize("adjustment", [-1, 0, 1])
def test_batch_byte_boundary_counts_utf8_wrapper_and_comma(text, adjustment):
    docs = documents(2)
    for doc in docs:
        doc["content"] = text * 20
    limit = len(encoded_payload(docs)) + adjustment
    batches = import_books.build_batches(docs, max_bytes=limit)
    assert [len(batch) for batch in batches] == ([1, 1] if adjustment < 0 else [2])
    assert [doc for batch in batches for doc in batch] == docs
    assert all(len(encoded_payload(batch)) <= limit for batch in batches)
    if text != "ascii":
        assert len(encoded_payload(docs)) > len(encoded_payload(docs).decode("utf-8"))


def test_batch_limit_includes_single_document_wrapper():
    docs = documents(1)
    exact = len(encoded_payload(docs))
    assert import_books.build_batches(docs, max_bytes=exact) == [docs]
    with pytest.raises(import_books.ImportFailure, match="book-1 exceeds"):
        import_books.build_batches(docs, max_bytes=exact - 1)


def test_document_at_actual_16mb_limit_and_one_byte_over():
    doc = documents(1)[0]
    doc["content"] = ""
    doc["content"] = "x" * (16_000_000 - len(encoded_payload([doc])))
    assert len(encoded_payload([doc])) == 16_000_000
    assert import_books.build_batches([doc]) == [[doc]]
    doc["content"] += "x"
    with pytest.raises(import_books.ImportFailure, match="book-1 exceeds"):
        import_books.build_batches([doc])


def test_custom_document_and_byte_limits_apply_to_every_reset_batch():
    docs = documents(9)
    for doc in docs:
        doc["content"] = "世界📚" * 20
    limit = len(encoded_payload(docs[:2]))
    batches = import_books.build_batches(docs, max_docs=3, max_bytes=limit)
    assert [len(batch) for batch in batches] == [2, 2, 2, 2, 1]
    assert [doc for batch in batches for doc in batch] == docs
    assert all(len(encoded_payload(batch)) <= limit for batch in batches)
    assert import_books.build_batches(docs, max_docs=1) == [[doc] for doc in docs]


@pytest.mark.parametrize("limits", [
    {"max_docs": 0}, {"max_docs": -1}, {"max_docs": 1001},
    {"max_bytes": 0}, {"max_bytes": -1}, {"max_bytes": 16_000_001},
])
def test_invalid_batch_limits_rejected_even_without_documents(limits):
    with pytest.raises(import_books.ImportFailure, match="Batch limits"):
        import_books.build_batches([], **limits)


def test_target_normalization_preserves_explicit_account():
    actual = import_books.validate_target(
        TARGET["endpoint"] + "/", "books_2026-01", SUBSCRIPTION.upper(), TENANT.upper(),
    )
    assert actual == {**TARGET, "index": "books_2026-01"}


@pytest.mark.parametrize("endpoint", [
    "http://example-search.search.windows.net",
    "https://example-search.search.windows.net.evil.invalid",
    "https://user:password@example-search.search.windows.net",
    "https://example-search.search.windows.net:443",
    "https://example-search.search.windows.net/indexes/books",
    "https://example-search.search.windows.net//",
    "https://example-search.search.windows.net?redirect=evil",
    "https://example-search.search.windows.net#fragment",
    "https://example-search.search.azure.cn",
    "https://localhost", "https://127.0.0.1", "",
])
def test_unsafe_endpoints_rejected(endpoint):
    with pytest.raises(import_books.ImportFailure, match="Endpoint must"):
        import_books.validate_target(endpoint, "books", SUBSCRIPTION, TENANT)


@pytest.mark.parametrize("index", ["", "Books", "-books", "../books", "books/path", "a?b", "a b", "a" * 129])
def test_invalid_index_names_rejected(index):
    with pytest.raises(import_books.ImportFailure, match="index name"):
        import_books.validate_target(TARGET["endpoint"], index, SUBSCRIPTION, TENANT)


@pytest.mark.parametrize("field", ["subscription", "tenant"])
@pytest.mark.parametrize("value", ["", "default", "0000", SUBSCRIPTION + "x", " " + SUBSCRIPTION])
def test_explicit_subscription_and_tenant_uuids_required(field, value):
    target = {**TARGET, field: value}
    with pytest.raises(import_books.ImportFailure, match="explicit UUIDs"):
        import_books.validate_target(**target)


def test_access_token_checks_account_then_requests_search_scoped_token():
    with patch.object(import_books, "az_json", side_effect=[
        account_response(id=SUBSCRIPTION.upper(), tenantId=TENANT.upper()),
        {"accessToken": SECRET, "subscription": SUBSCRIPTION, "tenant": TENANT},
    ]) as cli:
        assert import_books.access_token(TARGET) == SECRET
    assert cli.call_args_list == [
        call("account", "show"),
        call("account", "get-access-token", "--subscription", SUBSCRIPTION,
             "--resource", "https://search.azure.com"),
    ]


@pytest.mark.parametrize("account", [
    None, [], {}, account_response(id=TENANT), account_response(tenantId=SUBSCRIPTION),
    account_response(state="Disabled"), account_response(environmentName="AzureChinaCloud"),
    account_response(state=None), account_response(environmentName=None),
])
def test_account_mismatch_fails_before_token_acquisition(account):
    with patch.object(import_books, "az_json", return_value=account) as cli:
        with pytest.raises(import_books.ImportFailure, match="Active Azure CLI account"):
            import_books.access_token(TARGET)
    cli.assert_called_once_with("account", "show")


@pytest.mark.parametrize("response", [
    None, [], {}, {"accessToken": None}, {"accessToken": 123}, {"accessToken": ""},
    {"accessToken": f"{SECRET} "}, {"accessToken": f"{SECRET}\n"}, {"accessToken": "a\tb"},
])
def test_malformed_credentials_are_rejected_without_echoing_secrets(response):
    if isinstance(response, dict):
        response = {"subscription": SUBSCRIPTION, "tenant": TENANT, **response}
    with patch.object(import_books, "az_json", side_effect=[account_response(), response]):
        with pytest.raises(import_books.ImportFailure) as caught:
            import_books.access_token(TARGET)
    assert "token" in str(caught.value).lower()
    assert SECRET not in str(caught.value)


@pytest.mark.parametrize("changes", [
    {"subscription": None}, {"subscription": TENANT},
    {"tenant": None}, {"tenant": SUBSCRIPTION},
])
def test_token_target_mismatch_is_rejected(changes):
    response = {"accessToken": SECRET, "subscription": SUBSCRIPTION, "tenant": TENANT, **changes}
    with patch.object(import_books, "az_json", side_effect=[account_response(), response]):
        with pytest.raises(import_books.ImportFailure, match="token subscription/tenant") as caught:
            import_books.access_token(TARGET)
    assert SECRET not in str(caught.value)


@pytest.mark.parametrize("error", [
    OSError(SECRET),
    subprocess.CalledProcessError(1, ["az", SECRET], output=SECRET, stderr=SECRET),
    subprocess.TimeoutExpired(["az", SECRET], 30, output=SECRET, stderr=SECRET),
])
def test_cli_errors_are_sanitized(error, capsys):
    with patch.object(subprocess, "run", side_effect=error):
        with pytest.raises(import_books.ImportFailure, match="No CLI output logged") as caught:
            import_books.az_json("account", "show")
    assert SECRET not in str(caught.value)
    assert SECRET not in "".join(capsys.readouterr())


def test_cli_uses_bounded_noninteractive_json_command_and_sanitizes_bad_json():
    with patch.object(subprocess, "run", return_value=Mock(stdout=b'{"id":"example"}')) as cli:
        assert import_books.az_json("account", "show") == {"id": "example"}
    cli.assert_called_once_with(
        ["az", "account", "show", "--output", "json", "--only-show-errors"],
        capture_output=True, check=True, timeout=30,
    )
    with patch.object(subprocess, "run", return_value=Mock(stdout=SECRET)):
        with pytest.raises(import_books.ImportFailure, match="No CLI output logged") as caught:
            import_books.az_json("account", "show")
    assert SECRET not in str(caught.value)


@pytest.fixture
def transport():
    opener = Mock()
    with patch.object(urllib.request, "build_opener", return_value=opener) as builder:
        client = import_books.SearchClient(TARGET)
    builder.assert_called_once()
    assert isinstance(builder.call_args.args[0], import_books.NoRedirect)
    with patch.object(import_books, "access_token", return_value=SECRET) as token:
        yield client, opener, token


def respond(opener, value, status=200):
    stream = io.BytesIO(json.dumps(value, ensure_ascii=False).encode("utf-8"))
    stream.status = status
    opener.open.return_value = stream
    return stream


@pytest.mark.parametrize("operation,payload,response,status", [
    ("docs/index", b'{"value":[]}', {"value": []}, 200),
    ("docs/index", b'{"value":[]}', {"value": []}, 207),
    ("docs/search", b'{"search":"*"}', {"value": []}, 200),
    ("docs/book-42", None, {"id": "book-42", "title": "世界"}, 200),
    ("docs/$count", None, 10_000, 200),
])
def test_search_client_exact_request_and_response(transport, operation, payload, response, status):
    client, opener, token = transport
    stream = respond(opener, response, status)
    assert client.request(operation, payload) == response
    token.assert_called_once_with(TARGET)
    opener.open.assert_called_once()
    request = opener.open.call_args.args[0]
    assert request.full_url == (
        f"{TARGET['endpoint']}/indexes/books/{operation}?api-version=2025-09-01"
    )
    assert request.get_method() == ("POST" if payload is not None else "GET")
    assert request.data == payload
    assert request.get_header("Authorization") == f"Bearer {SECRET}"
    assert request.get_header("Content-type") == "application/json"
    assert request.get_header("Accept") == "application/json"
    assert opener.open.call_args.kwargs == {"timeout": 30}
    assert stream.closed


@pytest.mark.parametrize("operation,payload", [
    ("indexes", None), ("docs", None), ("docs/book-0", None),
    ("docs/book-01", None), ("docs/book-1/../../indexes", None),
    ("docs/book-1?redirect=evil", None), ("https://evil.invalid", None),
    ("docs/index", None), ("docs/search", None),
    ("docs/$count", b"{}"), ("docs/book-1", b"{}"),
])
def test_client_rejects_unapproved_operations_before_credentials(transport, operation, payload):
    client, opener, token = transport
    with pytest.raises(import_books.ImportFailure):
        client.request(operation, payload)
    token.assert_not_called()
    opener.open.assert_not_called()


@pytest.mark.parametrize("status", [201, 202, 204, 207, 301, 302, 307, 308, 400, 500])
def test_non_index_requests_require_http_200(transport, status):
    client, opener, _ = transport
    respond(opener, {"secret": SECRET}, status)
    with pytest.raises(import_books.ImportFailure, match=f"HTTP status {status}") as caught:
        client.request("docs/$count")
    assert SECRET not in str(caught.value)


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_redirect_handler_never_constructs_followup_request(code):
    request = urllib.request.Request(TARGET["endpoint"], headers={"Authorization": f"Bearer {SECRET}"})
    assert import_books.NoRedirect().redirect_request(
        request, io.BytesIO(), code, "redirect",
        {"Location": "https://evil.invalid"}, "https://evil.invalid",
    ) is None


@pytest.mark.parametrize("code", [301, 302, 307, 308, 401, 403, 404, 429, 500])
def test_http_errors_do_not_follow_redirects_retry_or_disclose_body(transport, code):
    client, opener, _ = transport
    body = io.BytesIO(SECRET.encode())
    opener.open.side_effect = urllib.error.HTTPError(
        "https://evil.invalid/" + SECRET, code, SECRET, {"Location": "https://evil.invalid"}, body,
    )
    with pytest.raises(import_books.SearchHTTPFailure, match=f"Search docs/\\$count failed with HTTP {code}") as caught:
        client.request("docs/$count")
    assert isinstance(caught.value, import_books.ImportFailure)
    assert caught.value.status == code
    opener.open.assert_called_once()
    assert body.tell() == 0
    assert SECRET not in str(caught.value)
    assert caught.value.__suppress_context__


@pytest.mark.parametrize("error", [OSError(SECRET), urllib.error.URLError(SECRET)])
def test_transport_errors_are_sanitized_without_replaying_writes(transport, error):
    client, opener, _ = transport
    opener.open.side_effect = error
    with pytest.raises(import_books.ImportFailure, match="outcome may be uncertain") as caught:
        client.request("docs/index", encoded_payload(documents(1)))
    opener.open.assert_called_once()
    assert SECRET not in str(caught.value)


def test_search_invalid_json_is_sanitized(transport):
    client, opener, _ = transport
    stream = io.BytesIO(SECRET.encode())
    stream.status = 200
    opener.open.return_value = stream
    with pytest.raises(import_books.ImportFailure, match="transport/JSON failure") as caught:
        client.request("docs/$count")
    assert SECRET not in str(caught.value)
    assert stream.closed


def test_credential_failure_prevents_http_request(transport):
    client, opener, token = transport
    token.side_effect = import_books.ImportFailure("Missing or malformed Azure CLI token.")
    with pytest.raises(import_books.ImportFailure, match="token"):
        client.request("docs/$count")
    opener.open.assert_not_called()


def test_upload_accepts_reordered_individual_200_and_201_results():
    docs = documents()
    response = upload_response(docs)
    response["value"][0]["statusCode"] = 200
    response["value"].reverse()
    assert import_books.check_upload_response(response, docs) is None


@pytest.mark.parametrize("status,code", [
    (False, 400), (False, 409), (False, 422), (False, 429), (False, 503),
    (False, 200), (True, 202), (True, 207), (True, 500),
    (True, "201"), (True, None), (1, 201), ("true", 201), (None, 201),
])
def test_partial_upload_statuses_rejected_and_error_messages_not_echoed(status, code):
    docs = documents()
    response = upload_response(docs)
    response["value"][1].update(status=status, statusCode=code, errorMessage=SECRET)
    with pytest.raises(import_books.ImportFailure, match="1 failed, 0 missing") as caught:
        import_books.check_upload_response(response, docs)
    assert "book-2 (HTTP " in str(caught.value)
    assert "Prior writes may exist" in str(caught.value)
    assert SECRET not in str(caught.value)


@pytest.mark.parametrize("response", [None, [], {}, {"value": None}, {"value": {}}, {"value": SECRET}])
def test_upload_requires_per_document_result_array(response):
    with pytest.raises(import_books.ImportFailure, match="no per-document results"):
        import_books.check_upload_response(response, documents())


@pytest.mark.parametrize("change", [
    "missing", "duplicate", "unexpected", "not-dict", "missing-key", "non-string-key",
    "missing-status", "missing-code", "empty",
])
def test_incomplete_duplicate_unexpected_or_malformed_upload_results_rejected(change):
    docs = documents()
    response = upload_response(docs)
    values = response["value"]
    if change == "missing":
        values.pop()
    elif change == "duplicate":
        values.append(copy.deepcopy(values[0]))
    elif change == "unexpected":
        values.append({"key": "book-999", "status": True, "statusCode": 201})
    elif change == "not-dict":
        values.append(SECRET)
    elif change == "missing-key":
        del values[0]["key"]
    elif change == "non-string-key":
        values[0]["key"] = ["book-1"]
    elif change == "missing-status":
        del values[0]["status"]
    elif change == "missing-code":
        del values[0]["statusCode"]
    else:
        values.clear()
    with pytest.raises(import_books.ImportFailure, match="incomplete/duplicate results") as caught:
        import_books.check_upload_response(response, docs)
    assert SECRET not in str(caught.value)


def test_upload_failure_diagnostics_are_bounded_to_ten_safe_keys():
    docs = documents(12)
    response = upload_response(docs)
    for value in response["value"]:
        value.update(status=False, statusCode=503, errorMessage=SECRET)
    with pytest.raises(import_books.ImportFailure, match="12 failed, 0 missing") as caught:
        import_books.check_upload_response(response, docs)
    assert "book-10 (HTTP 503)" in str(caught.value)
    assert "book-11 (HTTP 503)" not in str(caught.value)
    assert SECRET not in str(caught.value)


def test_http_207_still_requires_all_document_statuses_to_succeed(transport):
    client, opener, _ = transport
    docs = documents(2)
    response = upload_response(docs)
    response["value"][1].update(status=False, statusCode=503)
    respond(opener, response, status=207)
    actual = client.request("docs/index", encoded_payload(docs))
    with pytest.raises(import_books.ImportFailure, match="book-2.*503"):
        import_books.check_upload_response(actual, docs)


@pytest.mark.parametrize("count", [1, 2, 3, 500, 501, 1001, 10_000])
def test_verification_checks_every_group_field_count_and_representative_get(count):
    docs = documents(count)
    groups = [docs[start:start + 500] for start in range(0, count, 500)]
    positions = sorted({0, count // 2, count - 1})
    responses = [
        {"value": [dict(doc, **{"@search.score": 1}) for doc in reversed(group)],
         "@odata.count": len(group)}
        for group in groups
    ]
    client = Mock()
    client.request.side_effect = responses + [docs[position] for position in positions] + [count + 7]
    result = import_books.verify_documents(client, docs)
    expected_calls = [
        call("docs/search", json.dumps({
            "search": "*",
            "filter": "search.in(id, '" + ",".join(doc["id"] for doc in group) + "', ',')",
            "select": "id,title,content,url", "top": len(group), "count": True,
        }, separators=(",", ":")).encode())
        for group in groups
    ]
    expected_calls += [call(f"docs/{docs[position]['id']}") for position in positions]
    expected_calls.append(call("docs/$count"))
    assert client.request.call_args_list == expected_calls
    assert result == {
        "verified_book_count": count, "index_document_count": count + 7,
        "examples": [
            {field: docs[position][field] for field in ("id", "title", "url")}
            for position in positions
        ],
    }


def test_eventual_consistency_retries_missing_and_stale_fields_until_exact_match():
    docs = documents(3)
    stale = search_response(docs)
    stale["value"][1]["content"] = "Old content"
    client = Mock()
    client.request.side_effect = [
        search_response(docs[:1]), stale, search_response(docs), *docs, len(docs),
    ]
    with patch.object(import_books.time, "monotonic", side_effect=[100, 101, 101, 103, 103]), patch.object(
        import_books.time, "sleep",
    ) as sleep:
        result = import_books.verify_documents(client, docs, timeout=10)
    assert result["verified_book_count"] == 3
    assert client.request.call_args_list[0] == client.request.call_args_list[1]
    assert client.request.call_args_list[1] == client.request.call_args_list[2]
    assert sleep.call_args_list == [call(2), call(2)]
    assert client.request.call_count == 7


@pytest.mark.parametrize("field", ["id", "title", "content", "url"])
@pytest.mark.parametrize("missing", [False, True])
def test_verification_rejects_each_mismatched_or_missing_mapped_field(field, missing):
    docs = documents()
    response = search_response(docs)
    if missing:
        del response["value"][1][field]
    else:
        response["value"][1][field] = "wrong"
    client = Mock()
    client.request.return_value = response
    with patch.object(import_books.time, "monotonic", side_effect=[100, 105]):
        with pytest.raises(import_books.ImportFailure, match="Timed out verifying book records 1-3"):
            import_books.verify_documents(client, docs, timeout=5)
    assert client.request.call_count == 1


@pytest.mark.parametrize("change", ["missing", "duplicate", "unexpected", "wrong-count", "missing-count", "string-count"])
def test_verification_requires_exact_keys_cardinality_and_odata_count(change):
    docs = documents()
    response = search_response(docs)
    if change == "missing":
        response["value"].pop()
    elif change == "duplicate":
        response["value"].append(copy.deepcopy(docs[0]))
    elif change == "unexpected":
        response["value"].append(documents(4)[-1])
    elif change == "wrong-count":
        response["@odata.count"] += 1
    elif change == "missing-count":
        del response["@odata.count"]
    else:
        response["@odata.count"] = str(len(docs))
    client = Mock()
    client.request.return_value = response
    with patch.object(import_books.time, "monotonic", side_effect=[0, 1]):
        with pytest.raises(import_books.ImportFailure, match="missing or mismatched indexed fields"):
            import_books.verify_documents(client, docs, timeout=1)
    assert client.request.call_count == 1


@pytest.mark.parametrize("count", [True, 1.0])
def test_verification_rejects_noninteger_odata_count_even_for_one_book(count):
    docs = documents(1)
    response = search_response(docs)
    response["@odata.count"] = count
    client = Mock()
    client.request.side_effect = [response, docs[0], 1]
    with patch.object(import_books.time, "monotonic", side_effect=[0, 1]):
        with pytest.raises(import_books.ImportFailure, match="Timed out verifying book records 1-1"):
            import_books.verify_documents(client, docs, timeout=1)
    assert client.request.call_count == 1


@pytest.mark.parametrize("response", [None, [], {}, {"value": None}, {"value": {}}, {"value": [None]}])
def test_malformed_verification_response_fails_immediately(response):
    client = Mock()
    client.request.return_value = response
    with pytest.raises(import_books.ImportFailure, match="Malformed verification response"):
        import_books.verify_documents(client, documents())
    assert client.request.call_count == 1


def test_verification_uses_one_deadline_across_groups_and_bounds_final_sleep():
    docs = documents(501)
    client = Mock()
    client.request.side_effect = [search_response(docs[:500]), search_response([]), search_response([])]
    with patch.object(import_books.time, "monotonic", side_effect=[100, 104.5, 104.75, 105]), patch.object(
        import_books.time, "sleep",
    ) as sleep:
        with pytest.raises(import_books.ImportFailure, match="book records 501-501"):
            import_books.verify_documents(client, docs, timeout=5)
    sleep.assert_called_once_with(0.25)
    assert client.request.call_count == 3
    assert client.request.call_args_list[1] == client.request.call_args_list[2]


@pytest.mark.parametrize("field", ["id", "title", "content", "url"])
@pytest.mark.parametrize("position", [0, 1, 2])
def test_each_representative_get_must_match_all_mapped_fields(field, position):
    docs = documents()
    samples = copy.deepcopy(docs)
    samples[position][field] = "wrong"
    client = Mock()
    client.request.side_effect = [search_response(docs), *samples]
    with patch.object(import_books.time, "monotonic", side_effect=[100, 105]):
        with pytest.raises(import_books.ImportFailure, match=f"Exact-key readback mismatch for book-{position + 1}"):
            import_books.verify_documents(client, docs, timeout=5)
    assert client.request.call_count == position + 2
    assert client.request.call_args == call(f"docs/book-{position + 1}")


@pytest.mark.parametrize("response", [None, [], {}, "book-1"])
def test_malformed_representative_get_rejected(response):
    docs = documents(1)
    client = Mock()
    client.request.side_effect = [search_response(docs), response]
    with patch.object(import_books.time, "monotonic", side_effect=[100, 105]):
        with pytest.raises(import_books.ImportFailure, match="Exact-key readback mismatch"):
            import_books.verify_documents(client, docs, timeout=5)
    assert client.request.call_count == 2


@pytest.mark.parametrize("count", [-1, True, False, 3.0, "3", None, {}, []])
def test_malformed_final_index_count_fails_immediately(count):
    docs = documents()
    client = Mock()
    client.request.side_effect = [search_response(docs), *docs, count]
    with patch.object(import_books.time, "monotonic", return_value=100) as clock:
        with pytest.raises(import_books.ImportFailure, match="or malformed"):
            import_books.verify_documents(client, docs, timeout=5)
    clock.assert_called_once_with()
    assert client.request.call_count == 5
    assert client.request.call_args == call("docs/$count")


@pytest.mark.parametrize("count", [0, 2])
def test_small_final_index_count_fails_when_deadline_exhausted(count):
    docs = documents()
    client = Mock()
    client.request.side_effect = [search_response(docs), *docs, count]
    with patch.object(import_books.time, "monotonic", side_effect=[100, 105]):
        with pytest.raises(import_books.ImportFailure, match="Index count.*verification timed out"):
            import_books.verify_documents(client, docs, timeout=5)
    assert client.request.call_count == 5
    assert client.request.call_args == call("docs/$count")


@pytest.mark.parametrize("field", ["id", "title", "content", "url"])
@pytest.mark.parametrize("position", [0, 1, 2])
def test_stale_representative_get_converges_without_repeating_verified_reads(field, position):
    docs = documents()
    stale = {**docs[position], field: "stale"}
    client = Mock()
    client.request.side_effect = [
        search_response(docs), *docs[:position], stale, stale, *docs[position:], len(docs),
    ]
    with patch.object(import_books.time, "monotonic", side_effect=[100, 101, 101, 103, 103]), patch.object(
        import_books.time, "sleep",
    ) as sleep:
        result = import_books.verify_documents(client, docs, timeout=10)
    assert client.request.call_args_list[1:] == [
        *[call(f"docs/{doc['id']}") for doc in docs[:position]],
        *[call(f"docs/{docs[position]['id']}")] * 2,
        *[call(f"docs/{doc['id']}") for doc in docs[position:]],
        call("docs/$count"),
    ]
    assert sleep.call_args_list == [call(2), call(2)]
    assert result == {
        "verified_book_count": len(docs), "index_document_count": len(docs),
        "examples": [{field: doc[field] for field in ("id", "title", "url")} for doc in docs],
    }


def test_representative_get_http_404_converges():
    docs = documents(1)
    client = Mock()
    client.request.side_effect = [
        search_response(docs),
        import_books.SearchHTTPFailure("docs/book-1", 404),
        import_books.SearchHTTPFailure("docs/book-1", 404),
        docs[0], 1,
    ]
    with patch.object(import_books.time, "monotonic", side_effect=[100, 101, 101, 103, 103]), patch.object(
        import_books.time, "sleep",
    ) as sleep:
        result = import_books.verify_documents(client, docs, timeout=10)
    assert client.request.call_args_list[1:] == [call("docs/book-1")] * 3 + [call("docs/$count")]
    assert sleep.call_args_list == [call(2), call(2)]
    assert result["verified_book_count"] == result["index_document_count"] == 1


def test_representative_get_http_403_fails_without_retry():
    docs = documents(1)
    client = Mock()
    failure = import_books.SearchHTTPFailure("docs/book-1", 403)
    client.request.side_effect = [search_response(docs), failure]
    with patch.object(import_books.time, "monotonic", return_value=100) as clock:
        with pytest.raises(import_books.SearchHTTPFailure, match="HTTP 403") as caught:
            import_books.verify_documents(client, docs, timeout=10)
    assert caught.value is failure
    clock.assert_called_once_with()
    assert client.request.call_count == 2
    assert client.request.call_args == call("docs/book-1")


@pytest.mark.parametrize("final_count", [3, 10])
def test_final_index_count_converges_from_small_nonnegative_integers(final_count):
    docs = documents()
    client = Mock()
    client.request.side_effect = [search_response(docs), *docs, 0, 2, final_count]
    with patch.object(import_books.time, "monotonic", side_effect=[100, 101, 101, 103, 103]), patch.object(
        import_books.time, "sleep",
    ) as sleep:
        result = import_books.verify_documents(client, docs, timeout=10)
    assert client.request.call_count == 7
    assert client.request.call_args_list[-3:] == [call("docs/$count")] * 3
    assert sleep.call_args_list == [call(2), call(2)]
    assert result["verified_book_count"] == len(docs)
    assert result["index_document_count"] == final_count


@pytest.mark.parametrize("phase", ["stale-get", "404-get", "count"])
def test_search_exact_get_and_count_share_one_deadline_with_bounded_sleep(phase):
    docs = documents(1)
    client = Mock()
    stale = {**docs[0], "content": "stale"}
    if phase == "count":
        responses = [stale, docs[0], 0, 0]
        operation = "docs/$count"
        message = "Index count.*verification timed out"
    else:
        response = stale if phase == "stale-get" else import_books.SearchHTTPFailure("docs/book-1", 404)
        responses = [response, response, response]
        operation = "docs/book-1"
        message = "Exact-key readback mismatch.*verification timed out"
    client.request.side_effect = [search_response([]), search_response(docs), *responses]
    with patch.object(
        import_books.time, "monotonic", side_effect=[100, 101, 101, 103, 103, 104.5, 104.75, 105],
    ) as clock, patch.object(import_books.time, "sleep") as sleep:
        with pytest.raises(import_books.ImportFailure, match=message):
            import_books.verify_documents(client, docs, timeout=5)
    assert clock.call_count == 8
    assert sleep.call_args_list == [call(2), call(2), call(0.25)]
    assert client.request.call_count == (6 if phase == "count" else 5)
    assert client.request.call_args_list[0] == client.request.call_args_list[1]
    assert client.request.call_args_list[-2:] == [call(operation)] * 2


@pytest.fixture
def run_environment():
    docs = documents(3)
    client = Mock()
    with (
        patch.object(import_books, "load_documents", return_value=(docs, PINNED_SHA256)) as load,
        patch.object(import_books, "SearchClient", return_value=client) as factory,
        patch.object(Path, "stat", return_value=Mock(st_size=1234)) as stat,
        patch.object(Path, "mkdir", autospec=PATH_MKDIR) as mkdir,
        patch.object(Path, "write_text", autospec=PATH_WRITE_TEXT) as write,
    ):
        yield argparse.Namespace(
            docs=docs, client=client, load=load, factory=factory,
            stat=stat, mkdir=mkdir, write=write,
        )


def test_dry_run_real_data_reports_exact_batches_without_any_azure_calls(real_csv):
    raw, _ = real_csv
    with (
        patch.object(Path, "read_bytes", return_value=raw),
        patch.object(Path, "stat", return_value=Mock(st_size=len(raw))),
        patch.object(Path, "mkdir") as mkdir,
        patch.object(Path, "write_text") as write,
        patch.object(import_books, "az_json") as cli,
        patch.object(import_books, "access_token") as token,
        patch.object(import_books, "SearchClient") as client,
    ):
        result = import_books.run(arguments(dry_run=True))
        mapped, _ = import_books.load_documents(Path("data/books.csv"))
    batches = [mapped[start:start + 1000] for start in range(0, 10_000, 1000)]
    assert result == {
        "mode": "dry-run", "source": PINNED_SOURCE, "sha256": PINNED_SHA256,
        "csv_bytes": 3_182_802, "rows": 10_000, "target": TARGET,
        "batch_count": 10, "largest_batch_bytes": max(len(encoded_payload(batch)) for batch in batches),
        "action": "mergeOrUpload",
    }
    for external in (mkdir, write, cli, token, client):
        external.assert_not_called()


def test_live_run_real_csv_round_trips_all_10000_books_entirely_in_memory(real_csv):
    raw, rows = real_csv
    expected = {doc["id"]: doc for doc in map(mapped_document, rows)}
    indexed = {}
    uploaded_ids = []
    searched_ids = []
    readback_ids = []
    unrelated_count = 2

    def request(operation, payload=None):
        if operation == "docs/$count":
            assert payload is None
            return len(indexed) + unrelated_count
        if operation == "docs/index":
            assert len(payload) <= 16_000_000
            actions = json.loads(payload)["value"]
            assert len(actions) == 1000
            batch = []
            for action in actions:
                assert action.pop("@search.action") == "mergeOrUpload"
                assert action == expected[action["id"]]
                assert action["id"] not in indexed
                indexed[action["id"]] = action
                uploaded_ids.append(action["id"])
                batch.append(action)
            return upload_response(batch)
        if operation == "docs/search":
            assert len(indexed) == 10_000
            query = json.loads(payload)
            assert query["search"] == "*"
            assert query["select"] == "id,title,content,url"
            assert query["count"] is True
            assert query["top"] == 500
            assert query["filter"].startswith("search.in(id, '")
            assert query["filter"].endswith("', ',')")
            ids = query["filter"][len("search.in(id, '"):-len("', ',')")].split(",")
            assert len(ids) == 500
            searched_ids.extend(ids)
            return search_response([indexed[key] for key in reversed(ids)])
        assert operation in ("docs/book-1", "docs/book-5001", "docs/book-10000")
        assert payload is None
        key = operation.removeprefix("docs/")
        readback_ids.append(key)
        return copy.deepcopy(indexed[key])

    client = Mock()
    client.request.side_effect = request
    with (
        patch.object(Path, "read_bytes", return_value=raw),
        patch.object(Path, "stat", return_value=Mock(st_size=len(raw))),
        patch.object(Path, "mkdir", autospec=PATH_MKDIR) as mkdir,
        patch.object(Path, "write_text", autospec=PATH_WRITE_TEXT) as write,
        patch.object(import_books, "SearchClient", return_value=client) as factory,
    ):
        result = import_books.run(arguments())
    assert indexed == expected
    assert uploaded_ids == searched_ids == list(expected)
    assert readback_ids == ["book-1", "book-5001", "book-10000"]
    assert client.request.call_count == 35
    assert client.request.call_args_list[0] == client.request.call_args_list[-1] == call("docs/$count")
    factory.assert_called_once_with(TARGET)
    assert result["rows"] == result["uploaded_document_count"] == result["verified_book_count"] == 10_000
    assert result["index_document_count_before"] == unrelated_count
    assert result["index_document_count"] == 10_002
    assert result["batch_count"] == 10
    assert result["sha256"] == PINNED_SHA256
    assert result["examples"] == [
        {field: expected[key][field] for field in ("id", "title", "url")} for key in readback_ids
    ]
    path = import_books.ROOT / "deployment/.artifacts/books-import.json"
    mkdir.assert_called_once_with(path.parent, parents=True, exist_ok=True)
    write.assert_called_once_with(
        path, json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
    )


def test_run_rejects_changed_real_source_before_credentials_or_writes(real_csv):
    raw, _ = real_csv
    with (
        patch.object(Path, "read_bytes", return_value=raw + b"\n"),
        patch.object(Path, "mkdir") as mkdir,
        patch.object(Path, "write_text") as write,
        patch.object(import_books, "SearchClient") as client,
        patch.object(import_books, "access_token") as token,
    ):
        with pytest.raises(import_books.ImportFailure, match="SHA256 mismatch"):
            import_books.run(arguments())
    for external in (mkdir, write, client, token):
        external.assert_not_called()


def test_live_run_uploads_all_batches_verifies_then_writes_complete_report(run_environment):
    env = run_environment
    batches = [env.docs[:2], env.docs[2:]]
    verification = {"verified_book_count": 3, "index_document_count": 10, "examples": [{"id": "book-1"}]}
    events = Mock()
    events.attach_mock(env.load, "load")
    events.attach_mock(env.factory, "create_client")
    events.attach_mock(env.client.request, "request")
    events.attach_mock(env.mkdir, "mkdir")
    events.attach_mock(env.write, "write")
    env.client.request.side_effect = [7, *[upload_response(batch) for batch in batches]]
    with patch.object(import_books, "build_batches", return_value=batches) as build, patch.object(
        import_books, "verify_documents", return_value=verification,
    ) as verify:
        events.attach_mock(build, "build")
        events.attach_mock(verify, "verify")
        result = import_books.run(arguments())
    assert [event[0] for event in events.mock_calls] == [
        "load", "build", "create_client", "request", "request", "request", "verify", "mkdir", "write",
    ]
    env.load.assert_called_once_with(import_books.ROOT / "data/books.csv")
    build.assert_called_once_with(env.docs)
    env.factory.assert_called_once_with(TARGET)
    assert env.client.request.call_args_list == [
        call("docs/$count"), *[call("docs/index", encoded_payload(batch)) for batch in batches],
    ]
    verify.assert_called_once_with(env.client, env.docs, 30)
    timestamp = datetime.datetime.fromisoformat(result["verified_at_utc"])
    assert timestamp.utcoffset() == datetime.timedelta(0)
    assert result == {
        "mode": "upload", "source": PINNED_SOURCE, "sha256": PINNED_SHA256,
        "csv_bytes": 1234, "rows": 3, "target": TARGET,
        "batch_count": 2, "largest_batch_bytes": max(len(encoded_payload(batch)) for batch in batches),
        "action": "mergeOrUpload", "index_document_count_before": 7,
        **verification, "uploaded_document_count": 3, "verified_at_utc": result["verified_at_utc"],
    }
    path = import_books.ROOT / "deployment/.artifacts/books-import.json"
    env.mkdir.assert_called_once_with(path.parent, parents=True, exist_ok=True)
    env.write.assert_called_once_with(
        path, json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
    )


@pytest.mark.parametrize("failure", ["target", "csv", "late-giant-document", "stat"])
def test_all_local_data_validation_finishes_before_cloud_calls(run_environment, failure):
    env = run_environment
    args = arguments()
    if failure == "target":
        args.endpoint = "https://evil.invalid"
    elif failure == "csv":
        env.load.side_effect = import_books.ImportFailure("CSV SHA256 mismatch")
    elif failure == "late-giant-document":
        env.docs.extend(documents(1001))
        env.docs[-1] = {**env.docs[-1], "id": "book-99999", "content": "x" * 16_000_000}
    else:
        env.stat.side_effect = OSError("cannot stat source")
    with pytest.raises((import_books.ImportFailure, OSError)):
        import_books.run(args)
    env.factory.assert_not_called()
    env.client.request.assert_not_called()
    env.mkdir.assert_not_called()
    env.write.assert_not_called()
    if failure == "target":
        env.load.assert_not_called()


@pytest.mark.parametrize("count", [-1, True, False, "0", 0.0, None, {}])
def test_invalid_preupload_count_prevents_all_writes(run_environment, count):
    env = run_environment
    env.client.request.return_value = count
    with pytest.raises(import_books.ImportFailure, match="Malformed pre-upload index count"):
        import_books.run(arguments())
    env.client.request.assert_called_once_with("docs/$count")
    env.mkdir.assert_not_called()
    env.write.assert_not_called()


def test_partial_upload_stops_later_batches_verification_and_report(run_environment):
    env = run_environment
    first = env.docs[:1]
    response = upload_response(first)
    response["value"][0].update(status=False, statusCode=503, errorMessage=SECRET)
    env.client.request.side_effect = [0, response]
    with patch.object(import_books, "build_batches", return_value=[first, env.docs[1:]]), patch.object(
        import_books, "verify_documents",
    ) as verify:
        with pytest.raises(import_books.ImportFailure, match="Prior writes may exist") as caught:
            import_books.run(arguments())
    assert env.client.request.call_args_list == [call("docs/$count"), call("docs/index", encoded_payload(first))]
    verify.assert_not_called()
    env.mkdir.assert_not_called()
    env.write.assert_not_called()
    assert SECRET not in str(caught.value)


def test_verification_failure_never_writes_success_report(run_environment):
    env = run_environment
    env.client.request.side_effect = [0, upload_response(env.docs)]
    with patch.object(import_books, "verify_documents", side_effect=import_books.ImportFailure("mismatched fields")):
        with pytest.raises(import_books.ImportFailure, match="mismatched fields"):
            import_books.run(arguments())
    env.mkdir.assert_not_called()
    env.write.assert_not_called()


@pytest.mark.parametrize("report", ["data/books.csv", "data/../data/books.csv"])
def test_report_cannot_overwrite_source_is_validated_before_any_cloud_call(run_environment, report):
    env = run_environment
    env.client.request.side_effect = [0, upload_response(env.docs)]
    with patch.object(import_books, "verify_documents", return_value={}):
        with pytest.raises(import_books.ImportFailure, match="Report cannot overwrite"):
            import_books.run(arguments(report=report))
    env.mkdir.assert_not_called()
    env.write.assert_not_called()
    env.factory.assert_not_called()
    env.client.request.assert_not_called()
    env.load.assert_not_called()


def test_dry_run_rejects_report_source_collision_too(run_environment):
    with pytest.raises(import_books.ImportFailure, match="Report cannot overwrite"):
        import_books.run(arguments(dry_run=True, report="data/books.csv"))
    env = run_environment
    for external in (env.load, env.factory, env.client.request, env.mkdir, env.write):
        external.assert_not_called()
