// PIN pad (templates/pin.html): the on-screen keys and a physical keypad.

let pin = '';
const MAX = 12;
function render() {
  document.getElementById('pin-display').textContent = '•'.repeat(pin.length);
  document.getElementById('pin-value').value = pin;
}
function pinPress(d) { if (pin.length < MAX) { pin += d; render(); } }
function pinBack() { pin = pin.slice(0, -1); render(); }
function pinClear() { pin = ''; render(); }
// Physical keyboard support (USB numpad, etc.)
document.addEventListener('keydown', (e) => {
  if (e.key >= '0' && e.key <= '9') pinPress(e.key);
  else if (e.key === 'Backspace') pinBack();
  else if (e.key === 'Enter') document.getElementById('pin-form').submit();
});
render();
