path = r'C:\hr-copilot-mvp\static\dictation.html'
with open(path, encoding='utf-8', errors='ignore') as f:
    content = f.read()

start = content.find('function updateWordCount')
if start == -1:
    raise SystemExit('no')

brace = content.find('{', start)
i = brace
depth = 0
end = -1
while i < len(content):
    if content[i] == '{':
        depth += 1
    elif content[i] == '}':
        depth -= 1
        if depth == 0:
            end = i + 1
            break
    i += 1

old = content[start:end]
new = '''function updateWordCount() {
  var lines = turns.map(function(t) {
    return speakerSerialLabel(t.type) + ': ' + t.text;
  });
  var area = document.getElementById('transcriptArea');
  if (area && currentSpeaker) {
    var draft = normalizeSpokenText(area.value);
    if (draft) lines.push(speakerSerialLabel(currentSpeaker) + ': ' + draft);
  }
  var text = lines.join('\\n');
  var trimmed = text.trim();
  var count = trimmed ? trimmed.split(/\\s+/).length : 0;
  var el = document.getElementById('wordCount');
  if (el) el.textContent = count + ' words · ' + text.length + ' characters';
  var saveBtn = document.getElementById('saveBtn');
  if (saveBtn && saveBtn.textContent !== 'Saving...') {
    if (area) {
      saveBtn.disabled = !area.value.trim();
    } else {
      saveBtn.disabled = !trimmed;
    }
  }
  updateSaveState();
  updateOkState();
}'''

content = content.replace(old, new)
with open(path, 'w', encoding='utf-8') as f:
    f.write(content)

print('done')
