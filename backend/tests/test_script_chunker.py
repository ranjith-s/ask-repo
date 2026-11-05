"""JavaScript and TypeScript are split along their syntax tree, like Python."""

import pytest

from app.ingest.chunker import chunk_file, search_text
from app.models import SourceFile

TS = """import { HTTPError } from './errors.js';
import type { Options } from './types.js';

/**
 * Retry a request with exponential backoff.
 */
export async function retry<T>(fn: () => Promise<T>, limit = 2): Promise<T> {
  return fn();
}

export const delay = (ms: number): Promise<void> =>
  new Promise((resolve) => setTimeout(resolve, ms));

@Injectable()
export class Client extends Base implements Closeable {
  private retries: number = 0;
  static readonly VERSION = '1.0';

  /** Send a GET request. */
  async get(url: string): Promise<Response> {
    return fetch(url);
  }

  #sign(request: Request): void {}

  get size(): number { return 1; }
}

export interface ClientOptions { timeout?: number }
export type Method = 'get' | 'post';
enum Color { Red, Green }

export default function createClient(options: ClientOptions) { return new Client(); }

client.on('error', console.error);
"""

TSX = """import React from 'react';

type Props = { name: string };

export const Greeting = ({ name }: Props) => {
  return <h1 className="greeting">Hello {name}</h1>;
};

export default function App() {
  return <Greeting name="world" />;
}
"""

COMMONJS = """'use strict';

var send = require('send');
const { Buffer } = require('node:buffer');

/**
 * Send a response.
 */
res.send = function send(body) {
  return this.end(body);
};

req.get =
req.header = function header(name) {
  return this.headers[name];
};

defineGetter(req, 'fresh', function () {
  return true;
});

Foo.prototype.bar = () => 1;

module.exports = res;
"""


def chunks_of(path: str, source: str) -> dict[str, list]:
    out: dict[str, list] = {}
    for chunk in chunk_file(SourceFile(path, source)):
        out.setdefault(chunk.symbol, []).append(chunk)
    return out


def line(source: str, number: int) -> str:
    return source.splitlines()[number - 1]


def test_typescript_definitions_become_named_chunks():
    chunks = chunks_of("src/client.ts", TS)
    kinds = {symbol: [c.kind for c in cs] for symbol, cs in chunks.items()}
    assert kinds["retry"] == ["function"] and kinds["delay"] == ["function"]
    assert kinds["Client"] == ["class"] and kinds["createClient"] == ["function"]
    assert (
        kinds["Client.get"] == ["method"] and kinds["Client.#sign"] == ["method"] and kinds["Client.size"] == ["method"]
    )
    assert kinds["ClientOptions"] == kinds["Method"] == kinds["Color"] == ["type"]
    assert kinds["src.client"] == ["module"]
    assert any(k == ["code"] for k in kinds.values())  # client.on(...) at the top level


def test_line_ranges_include_jsdoc_and_decorators():
    chunks = chunks_of("src/client.ts", TS)
    retry = chunks["retry"][0]
    assert line(TS, retry.start_line) == "/**" and "Retry a request" in retry.content
    assert line(TS, retry.end_line) == "}"
    client = chunks["Client"][0]
    assert line(TS, client.start_line) == "@Injectable()"
    get = chunks["Client.get"][0]
    assert "/** Send a GET request. */" in get.content and get.content.startswith("// in class Client extends Base")


def test_class_outline_lists_fields_and_signatures_without_bodies():
    outline = chunks_of("src/client.ts", TS)["Client"][0].content
    assert "private retries: number = 0" in outline and "static readonly VERSION = '1.0'" in outline
    assert "async get(url: string): Promise<Response>" in outline and "#sign(request: Request): void" in outline
    assert "return fetch(url)" not in outline


def test_module_outline_has_imports_and_signatures():
    outline = chunks_of("src/client.ts", TS)["src.client"][0].content
    assert "import type { Options } from './types.js';" in outline
    assert "export async function retry<T>(fn: () => Promise<T>, limit = 2): Promise<T>" in outline
    assert "@Injectable" not in outline  # decorators are dropped from signatures


def test_tsx_components_parse_with_the_tsx_grammar():
    chunks = chunks_of("src/App.tsx", TSX)
    assert chunks["Greeting"][0].kind == "function" and "<h1" in chunks["Greeting"][0].content
    assert chunks["App"][0].kind == "function" and chunks["Props"][0].kind == "type"


@pytest.mark.parametrize("path", ["lib/response.js", "lib/response.cjs"])
def test_commonjs_assignments_and_getters_are_definitions(path):
    chunks = chunks_of(path, COMMONJS)
    assert {"res.send", "req.get", "defineGetter fresh", "Foo.prototype.bar"} <= chunks.keys()
    send = chunks["res.send"][0]
    assert "Send a response." in send.content and line(COMMONJS, send.end_line) == "};"
    outline = next(c for cs in chunks.values() for c in cs if c.kind == "module").content
    assert "var send = require('send');" in outline and "res.send = function send(body)" in outline
    loose = [c.content for cs in chunks.values() for c in cs if c.kind == "code"]
    assert any("module.exports = res;" in text for text in loose)
    assert not any("use strict" in text for text in loose)


def test_route_handlers_are_named_by_method_and_path():
    app = "router.post('/login', async (req, res) => {\n  res.json({ ok: true });\n});\n"
    assert list(chunks_of("routes/auth.js", app)) == ["routes.auth", "router.post /login"]


def test_long_functions_are_windowed():
    body = "\n".join(f"  const x{i} = {i};" for i in range(300))
    windows = chunks_of("big.ts", f"function big() {{\n{body}\n}}\n")["big"]
    assert len(windows) > 1 and windows[1].start_line < windows[0].end_line and windows[-1].end_line == 302


def test_broken_code_still_produces_chunks():
    chunks = chunks_of("broken.ts", "export function ok() { return 1 }\nconst = = ;\nfunction alsoOk() {}\n")
    assert {"ok", "alsoOk"} <= chunks.keys()


def test_camel_case_identifiers_are_split_for_keyword_search():
    chunk = chunks_of("src/client.ts", TS)["retry"][0]
    words = search_text(chunk).split()
    assert {"retry", "exponential", "backoff"} <= set(words)
    js = chunks_of("lib/response.js", COMMONJS)["req.get"][0]
    assert "header" in search_text(js).split()
