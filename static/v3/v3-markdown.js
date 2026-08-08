/* Lumeri v3 message formatting. Kept independent so rendering rules can evolve without coupling to workspace state. */
(() => {
  function createRenderer({ escapeHTML }) {
  // ── Markdown renderer ───────────────────────────────────────────────

  function renderMarkdown(src) {
    if (!src) return "";
    const text = String(src);

    // Extract fenced code blocks before any other processing
    const codeBlocks = [];
    const withPlaceholders = text.replace(/^```(\w*)\n([\s\S]*?)^```/gm, (_, lang, code) => {
      const idx = codeBlocks.length;
      codeBlocks.push(`<pre class="md-code-block"><code class="lang-${escapeHTML(lang || "text")}">${escapeHTML(code.replace(/\n$/, ""))}</code></pre>`);
      return `\x00CB${idx}\x00`;
    });

    // Split into block-level chunks by double newline
    const blocks = withPlaceholders.split(/\n{2,}/);
    const out = [];
    let orderedListNextStart = null;

    for (let i = 0; i < blocks.length; i++) {
      const block = blocks[i];
      const orderedMarker = block.trim().match(/^(\d+)[.)]\s/);
      if (!orderedMarker) orderedListNextStart = null;

      // Code block placeholder
      if (/^\x00CB\d+\x00$/.test(block.trim())) {
        out.push(codeBlocks[+block.trim().slice(3, -1)]);
        continue;
      }

      // Heading
      const hm = block.match(/^(#{1,6})\s+(.+)$/m);
      if (hm && block.trim().startsWith("#")) {
        const lvl = hm[1].length;
        out.push(`<h${lvl} class="md-h">${mdInline(hm[2])}</h${lvl}>`);
        continue;
      }

      // Horizontal rule
      if (/^(\s*[-*_]){3,}\s*$/.test(block.trim())) {
        out.push(`<hr class="md-hr">`);
        continue;
      }

      // Blockquote
      if (block.trim().startsWith(">")) {
        const inner = block.replace(/^>\s?/gm, "");
        out.push(`<blockquote class="md-blockquote">${renderMarkdown(inner)}</blockquote>`);
        continue;
      }

      // Table
      const tableLines = block.trim().split("\n");
      if (tableLines.length >= 2 && tableLines[0].includes("|") && /^[\s|:-]+$/.test(tableLines[1])) {
        out.push(mdTable(tableLines));
        continue;
      }

      // Unordered list
      if (/^[\t ]*[-*+]\s/.test(block.trim())) {
        out.push(mdList(block, "ul"));
        continue;
      }

      // Ordered list
      if (orderedMarker) {
        const requestedStart = Number(orderedMarker[1]) || 1;
        const start = orderedListNextStart !== null && requestedStart === 1
          ? orderedListNextStart
          : requestedStart;
        out.push(mdList(block, "ol", start));
        const itemCount = block.match(/^[\t ]*\d+[.)]\s+/gm)?.length || 1;
        orderedListNextStart = start + itemCount;
        continue;
      }

      // Paragraph (may contain inline code block placeholders on their own line)
      const lines = block.split("\n");
      const paraLines = [];
      for (const ln of lines) {
        if (/^\x00CB\d+\x00$/.test(ln.trim())) {
          if (paraLines.length) {
            out.push(`<p>${mdInline(paraLines.join("\n"))}</p>`);
            paraLines.length = 0;
          }
          out.push(codeBlocks[+ln.trim().slice(3, -1)]);
        } else {
          paraLines.push(ln);
        }
      }
      if (paraLines.length) {
        out.push(`<p>${mdInline(paraLines.join("\n"))}</p>`);
      }
    }
    return out.join("\n");
  }

  function mdInline(s) {
    let r = escapeHTML(s);
    // Inline code (must come before bold/italic to avoid conflicts)
    r = r.replace(/`([^`\n]+?)`/g, '<code class="md-inline-code">$1</code>');
    // Entity references — before bold/italic so underscore-delimited IDs
    // (v_001, s0_shot0) are not consumed by emphasis rules.
    r = r.replace(/\b(v_\d+|img_\d+|aud_\d+|lot_\d+)\b/g,
      '<span class="md-entity" data-entity-kind="asset" data-entity-id="$1" role="link" tabindex="0">$1</span>');
    r = r.replace(/\b(clip_[a-f0-9]{8,16})\b/g,
      '<span class="md-entity" data-entity-kind="clip" data-entity-id="$1" role="link" tabindex="0">$1</span>');
    r = r.replace(/\b(s\d+_shot\d+)\b/g,
      '<span class="md-entity" data-entity-kind="shot" data-entity-id="$1" role="link" tabindex="0">$1</span>');
    r = r.replace(/\b(scene\d+)\b/g,
      '<span class="md-entity" data-entity-kind="scene" data-entity-id="$1" role="link" tabindex="0">$1</span>');
    // Images
    r = r.replace(/!\[([^\]]*)\]\(([^)]+)\)/g, '<img class="md-img" alt="$1" src="$2">');
    // Links
    r = r.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a class="md-link" href="$2" target="_blank" rel="noopener">$1</a>');
    // Bold + italic
    r = r.replace(/\*\*\*(.+?)\*\*\*/g, "<strong><em>$1</em></strong>");
    // Bold
    r = r.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    r = r.replace(/__(.+?)__/g, "<strong>$1</strong>");
    // Italic
    r = r.replace(/\*(.+?)\*/g, "<em>$1</em>");
    r = r.replace(/_(.+?)_/g, "<em>$1</em>");
    // Strikethrough
    r = r.replace(/~~(.+?)~~/g, "<del>$1</del>");
    // Line break (trailing double space or backslash)
    r = r.replace(/  \n/g, "<br>");
    r = r.replace(/\\\n/g, "<br>");
    // Single newlines within a paragraph → <br>
    r = r.replace(/\n/g, "<br>");
    return r;
  }

  function mdList(block, tag, start = 1) {
    const lines = block.split("\n");
    const items = [];
    for (const ln of lines) {
      const m = tag === "ul"
        ? ln.match(/^[\t ]*[-*+]\s+(.*)/)
        : ln.match(/^[\t ]*\d+[.)]\s+(.*)/);
      if (m) items.push(`<li>${mdInline(m[1])}</li>`);
      else if (items.length) {
        items[items.length - 1] = items[items.length - 1].replace("</li>", `<br>${mdInline(ln.trim())}</li>`);
      }
    }
    const startAttr = tag === "ol" && start !== 1 ? ` start="${start}"` : "";
    return `<${tag} class="md-list"${startAttr}>${items.join("")}</${tag}>`;
  }

  function mdTable(lines) {
    const parseRow = (ln) => ln.replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
    const headers = parseRow(lines[0]);
    const alignRow = parseRow(lines[1]);
    const aligns = alignRow.map((c) => {
      if (c.startsWith(":") && c.endsWith(":")) return "center";
      if (c.endsWith(":")) return "right";
      return "left";
    });
    let html = '<table class="md-table"><thead><tr>';
    for (let i = 0; i < headers.length; i++) {
      html += `<th style="text-align:${aligns[i] || "left"}">${mdInline(headers[i])}</th>`;
    }
    html += "</tr></thead><tbody>";
    for (let r = 2; r < lines.length; r++) {
      if (!lines[r].trim()) continue;
      const cells = parseRow(lines[r]);
      html += "<tr>";
      for (let i = 0; i < headers.length; i++) {
        html += `<td style="text-align:${aligns[i] || "left"}">${mdInline(cells[i] || "")}</td>`;
      }
      html += "</tr>";
    }
    html += "</tbody></table>";
    return html;
  }


    return renderMarkdown;
  }

  window.LumeriV3Markdown = Object.freeze({ createRenderer });
})();
