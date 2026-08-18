import { readFile } from "node:fs/promises";
import { ESLint } from "eslint";

const html = await readFile(
  new URL("../app/static/index.html", import.meta.url),
  "utf8",
);
const match = html.match(/<script>([\s\S]*?)<\/script>/);

if (!match) {
  throw new Error("No inline dashboard script was found");
}

const eslint = new ESLint({
  overrideConfigFile: true,
  overrideConfig: {
    languageOptions: {
      ecmaVersion: "latest",
      sourceType: "script",
      globals: Object.fromEntries(
        [
          "AbortController",
          "clearTimeout",
          "console",
          "CSS",
          "devicePixelRatio",
          "document",
          "fetch",
          "FormData",
          "getComputedStyle",
          "Intl",
          "localStorage",
          "location",
          "Map",
          "navigator",
          "setTimeout",
          "window",
        ].map((name) => [name, "readonly"]),
      ),
    },
    rules: {
      "no-undef": "error",
      "no-unreachable": "error",
      "no-constant-condition": "error",
    },
  },
});

const [result] = await eslint.lintText(match[1], {
  filePath: "dashboard-inline.js",
});
const formatter = await eslint.loadFormatter("stylish");
const output = formatter.format([result]);

if (output) {
  process.stdout.write(output);
}
if (result.errorCount > 0) {
  process.exitCode = 1;
}
