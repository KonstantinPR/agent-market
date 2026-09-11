"""Проверка чистых JS-хелперов (fmt/q/…) через Node vm.

Выполняется node --check (синтаксис) + прогон функций в изолированном
контексте с заглушкой DOM. Если node недоступен — тест пропускается.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[2] / "app" / "static" / "app.js"

HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

const APP_PATH = process.argv[2];

const el = (sel) => ({
  value: "", checked: false, innerHTML: "", textContent: "", dataset: {},
  addEventListener() {}, appendChild() {}, setAttribute() {},
  classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
  style: {}, files: { length: 0 }, disabled: false,
});
const doc = {
  querySelector: () => el(), querySelectorAll: () => [],
  getElementById: () => el(), createElement: () => el(),
  addEventListener() {},
};

const lsStore = {};
const sandbox = {
  document: doc, window: { addEventListener() {} }, location: { search: "" },
  fetch: async () => ({ ok: true, json: async () => ({}) }),
  alert() {}, confirm: () => true, URLSearchParams,
  console, Object, Array, Number, String, Math, Date, JSON, RegExp, Map, Set,
  Promise, setTimeout, clearTimeout, setInterval, clearInterval,
  require, process, __filename, __dirname,
  localStorage: {
    getItem: (k) => (k in lsStore ? lsStore[k] : null),
    setItem: (k, v) => { lsStore[k] = String(v); },
    removeItem: (k) => { delete lsStore[k]; },
  },
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(APP_PATH, "utf8"), sandbox);

const { fmt, fmtMoney, fmtFloat, fmtPct, cls, qs, writeDbStorage, setWriteDbStorage } = sandbox;

const norm = (s) => s.replace(/[\u202F\u00A0]/g, " ");
assert.strictEqual(norm(fmt(1234)), "1 234", "fmt thousand separator");
assert.strictEqual(fmt(0), "0", "fmt zero");
assert.ok(fmtMoney(100).endsWith("\u20BD"));
assert.strictEqual(fmtPct(12.345), "12.3%", "fmtPct");
assert.strictEqual(fmtPct(""), "\u2014", "fmtPct empty");
assert.strictEqual(fmtPct("abc"), "\u2014", "fmtPct non-numeric");
assert.strictEqual(cls(-2), "neg", "cls negative");
assert.strictEqual(cls(5), "pos", "cls positive");
assert.strictEqual(
  qs({ a: 1, b: "", c: null, d: undefined, e: "x y" }),
  "?a=1&e=x+y",
  "qs drops empty and encodes"
);
assert.strictEqual(qs({}), "", "qs empty");

assert.strictEqual(writeDbStorage("wb", "cards"), true, "write-db default true");
setWriteDbStorage("wb", "cards", false);
assert.strictEqual(writeDbStorage("wb", "cards"), false, "write-db off persisted");
setWriteDbStorage("wb", "cards", true);
assert.strictEqual(writeDbStorage("wb", "cards"), true, "write-db back on");
assert.strictEqual(writeDbStorage("ozon", "realization"), true, "write-db default separate kind");

console.log("JS_TESTS_OK");
"""


@pytest.fixture()
def node_bin():
    return shutil.which("node")


def test_js_syntax(node_bin):
    if not node_bin:
        pytest.skip("node недоступен")
    res = subprocess.run([node_bin, "--check", str(APP_JS)],
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


def test_js_helpers(node_bin, tmp_path):
    if not node_bin:
        pytest.skip("node недоступен")
    assert APP_JS.exists(), str(APP_JS)
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    res = subprocess.run([node_bin, str(harness), str(APP_JS)],
                         capture_output=True, text=True)
    assert res.returncode == 0, f"{res.stdout}\n{res.stderr}"
    assert "JS_TESTS_OK" in res.stdout