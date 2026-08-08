# Lumeri Quanta interface contract

## Visual thesis

Quanta is a Lumeri workspace, not a separate demo language. It inherits Video's calm graphite Material workspace, shared header, modular desk, Agent rail, account flow, and Projects navigation, while replacing Video's ice-blue identity with a clean, luminous yellow-orange accent system. Its mark changes exactly one part of the shared Lumeri geometry: the original lower bar keeps its outer bounds but is split into a left point and a right bar; the upper row is unchanged. Matching point size, bar thickness, corner radius, and inner gap preserve the original rhythm. The structural anchor changes from a continuous timeline to a discrete state tree; the primary canvas shows the selected quantum without inventing render output that is not present in the project.

## Content plan

- Shared shell: Lumeri brand, account gate, Project/session navigation, Agent conversation, composer, tasks, files, and media library.
- Quanta desk: State Tree is the primary spatial anchor and owns the first full-width row. Preview becomes the discrete canvas; support modules share the row below it.
- Product truth: the tree and canvas are populated from the current Project's canonical `quanta` document. An empty Project renders an honest empty state.
- Evidence: the existing kernel demonstration remains available at `/quanta/demo`; `/quanta` is the product workspace.

## Interaction thesis

Quanta inherits Video's module add/hide, drag, resize, refresh, and Project/session switching behavior. Selecting a state in State Tree updates the discrete canvas. Agent operations refresh the canonical tree through the session API; UI state uses Quanta-specific browser-storage keys so Video and Quanta layouts cannot contaminate each other.

## Phase boundary

This phase is the loopback web workspace at `127.0.0.1:7788/quanta`. It does not include DMG packaging, iOS, Android, publishing, or a new design system.
