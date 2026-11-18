// Loads desktop/plugin.js outside Desktop and evaluates one expression against its exports.
// The host SDK and React are replaced by stubs: jsx() returns plain {type, props, key} objects,
// so pure helpers (and the shape of the element tree) can be checked without a browser.
//   node tests/pane_js_harness.mjs desktop/plugin.js '<expr using m.*>'   → prints JSON
import { readFileSync } from 'node:fs'

const [file, expr] = process.argv.slice(2)
const stubs = `
const sdk = { host: { navigate() {} }, useQuery: () => ({}) };
const useState = init => [typeof init === 'function' ? init() : init, () => {}];
const jsx = (type, props, key) => ({ type, props, key });
const jsxs = jsx;
`
const src = readFileSync(file, 'utf8')
  .replace(/^import \* as sdk from '@hermes\/plugin-sdk'$/m, '')
  .replace(/^import \{ useState \} from 'react'$/m, '')
  .replace(/^import \{ jsx, jsxs \} from 'react\/jsx-runtime'$/m, '')
if (/^import /m.test(src)) throw new Error('plugin.js has an import the harness does not stub')
const m = await import('data:text/javascript,' + encodeURIComponent(stubs + src))
const value = await new Function('m', `return (${expr})`)(m)
process.stdout.write(JSON.stringify(value ?? null))
