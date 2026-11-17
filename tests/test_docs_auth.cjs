// Regresión del formato pegado en el formulario; usa exclusivamente datos ficticios.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('api/static/docs/assets/docs.js', 'utf8');
const begin = source.indexOf('  function normalizeToken(');
const end = source.indexOf('  async function authorize(', begin);
const normalize = vm.runInNewContext('(' + source.slice(begin, end).trim() + ')');
test('normaliza valor, comillas, Bearer y asignación sin cambiar el secreto', () => {
  const token = 'token-ficticio_solo-para-pruebas';
  for (const input of [token, ` '${token}' `, `"${token}"`, `Bearer ${token}`, `Authorization: Bearer ${token}`, `IACP_API_TOKEN='${token}'`]) {
    assert.equal(normalize(input), token);
  }
});
test('rechaza múltiples líneas y espacios dentro del token', () => {
  assert.throws(() => normalize('uno dos'));
  assert.throws(() => normalize('IACP_API_TOKEN=uno\nOTRA_VARIABLE=dos'));
  assert.equal(normalize(''), '');
});
