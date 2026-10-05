const fs = require('node:fs');
const path = require('node:path');

const content = JSON.parse(fs.readFileSync(
  path.join(__dirname, '..', 'data', 'support.json'), 'utf8'));
const guidance = "This page describes the current product workflow; it is not an HR policy or a promise about every deployment. Before acting on information about an employee, verify the source record, consider context the system cannot see, and use your organization's process. If a control behaves differently from this guide, check the current workspace UI and confirm the saved result rather than assuming an action completed.";
const errors = [];

function words(value) {
  return (String(value).match(/\b[\w'-]+\b/g) || []).length;
}

function structuredText(value) {
  if (typeof value === 'string') return value;
  if (Array.isArray(value)) return value.map(structuredText).join(' ');
  if (value && typeof value === 'object') {
    return Object.entries(value)
      .filter(([key]) => key !== 'type')
      .map(([, item]) => structuredText(item))
      .join(' ');
  }
  return '';
}

for (const [slug, article] of Object.entries(content.articles)) {
  if (!slug || slug === 'faq') continue;
  const prose = [article.intro, guidance];
  for (const section of article.sections) prose.push(...section.paragraphs);
  let paragraphWords = words(prose.join(' '));
  let structuredWords = 0;
  let blockCount = 0;
  let calloutCount = 0;
  const typeNames = [];

  for (const section of article.sections) {
    const blocks = (article.presentation || {})[section.heading] || [];
    const kinds = {};
    let adjacentBlocks = 0;
    for (const block of blocks) {
      blockCount++;
      typeNames.push(block.type);
      if (block.type !== 'paragraph') {
        structuredWords += words(structuredText(block));
        adjacentBlocks++;
        if (adjacentBlocks > 2) {
          errors.push(`${slug}: more than two adjacent structured blocks in "${section.heading}"`);
        }
      } else {
        paragraphWords += words(structuredText(block));
        adjacentBlocks = 0;
      }
      kinds[block.type] = (kinds[block.type] || 0) + 1;
      if (kinds[block.type] > 1 && ['table', 'steps', 'bullets'].includes(block.type)) {
        errors.push(`${slug}: multiple ${block.type} blocks in "${section.heading}"`);
      }
      if (block.type === 'table') {
        if (block.headers.length > 5) errors.push(`${slug}: table has more than five columns`);
        if (block.headers.length < 2 || block.rows.length < 3 || block.rows.length > 8) {
          errors.push(`${slug}: table dimensions outside the supported range`);
        }
        if (block.rows.some(row => row.length !== block.headers.length)) {
          errors.push(`${slug}: table row does not match its header count`);
        }
      } else if (block.type === 'steps') {
        if (block.items.length < 1 || block.items.length > 8) {
          errors.push(`${slug}: steps block must contain 1-8 steps`);
        }
      } else if (block.type === 'bullets') {
        if (block.items.length < 3 || block.items.length > 7 ||
            block.items.some(item => words(item) >= 15)) {
          errors.push(`${slug}: bullets must contain 3-7 short items`);
        }
      } else if (block.type === 'callout') {
        calloutCount++;
        if (!['Note', 'Tip', 'Important'].includes(block.label)) {
          errors.push(`${slug}: callout needs a text label`);
        }
        if ((block.text.match(/[.!?](?:\s|$)/g) || []).length > 3) {
          errors.push(`${slug}: callout exceeds three sentences`);
        }
      } else if (!['paragraph', 'steps', 'bullets', 'table', 'callout'].includes(block.type)) {
        errors.push(`${slug}: unsupported content block ${block.type}`);
      }
    }
  }

  const total = paragraphWords + structuredWords;
  const paragraphShare = total ? paragraphWords / total : 1;
  if (paragraphShare < 0.65) errors.push(`${slug}: paragraph share is below 65%`);
  if (calloutCount > 2) errors.push(`${slug}: more than two callouts`);
  const label = slug || 'support overview';
  console.log(`${label}: ${[...new Set(typeNames)].join(', ') || 'no added blocks'}; ` +
    `${Math.round(paragraphShare * 100)}% paragraph / ${Math.round((1 - paragraphShare) * 100)}% structured` +
    ` (${blockCount} blocks)`);
}

if (errors.length) {
  console.error(errors.map(error => `ERROR: ${error}`).join('\n'));
  process.exitCode = 1;
}
