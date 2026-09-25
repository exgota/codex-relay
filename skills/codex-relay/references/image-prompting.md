# Image generation through Codex

`$CODEX_HOME` below defaults to `~/.codex`.

Codex has a built-in image tool (`image_gen`, backed by OpenAI's current GPT Image model). There's no CLI flag that calls it directly. It fires inside a model turn, so an image request is just a relay brief that asks Codex to generate. Codex saves every result to `$CODEX_HOME/generated_images/<thread id>/ig_<id>.png`, and the rollout records the exact prompt it sent as `revised_prompt` on an `image_generation_call` item.

Run image work as a relay task (`new`, or `send` to reuse a task) so the user can watch and pick variants in the app. Use headless `codex exec` only for a batch the user asked to run unwatched.

## Brief shape

Ask Codex to use the built-in image tool, then give the image prompt itself as labeled lines in this order. Leave out a field only when it truly doesn't apply.

```text
Use case: <ads-marketing | product-mockup | ui-asset | icon | sprite | illustration | photo>
Asset type: <the finished object and where it goes, aspect ratio, export size>
Primary request: <one clear creative job>
Input images: <Image 1 role + contract; Image 2 role + contract>
Scene/backdrop: <one committed stage>
Subject: <objects, people, and how they relate>
Style/medium: <how it was made + how it is reproduced or displayed>
Composition/framing: <ratio, hierarchy, scale, regions, crop, safe margins>
Lighting/mood: <light behavior; "X but not Y" mood fence>
Color palette: <dominant, support, accent>
Text (verbatim): <every visible string, counted; "no other readable text">
Typography: <family class, weight, hierarchy, placement, deliberate flaws>
Materials/textures: <surface behavior and wanted imperfection>
Constraints: <facts and invariants that must not drift>
Avoid: <this genre's specific clichés, never "low quality">
```

## Rules that held up

Observed in one user's image history (about 180 sessions): which prompts were reused or praised, and which were rejected. Counts show how many cases support each rule; treat them as field notes, not measurements.

1. **Cutouts go on a flat chroma key.** For sprites, icons and other assets that get background removal, use a perfectly flat `#FF00FF` or `#00FF00` background, and forbid that color, and anything close to it, in the subject. No shadows, gradients or floor. (2 praised outputs, reused in 10+ follow-up sessions.)
2. **Every reference image gets a contract.** Refer to it by index, and say what to borrow and what not to copy. Never lump sources, edit targets and style references into one "inspiration" bundle. (Explicit guidance plus 1 fully worked, blind-judged example.)
3. **Use the real image model for finished visuals.** Don't fake consistency with HTML, SVG, canvas or scripted compositing. Both clearest rejections came from doing that, and both were fixed by switching to a real `image_gen` call. (2 incidents.)
4. **A concrete action or object beats a mood adjective.** A physical action ("hands tearing bread") beat a vibe ("premium restaurant feel") in one blind-judged test. (1 test.)
5. **Text is opt-in, verbatim and counted.** Give each block exactly once, say "no other readable text", and put typography on its own line. (5+ worked examples.)
6. **Avoid lists name the genre's clichés.** For example: coupon clutter, fake gold bevels, floating ingredient confetti, plastic food, a generic delivery-app look. Generic quality words do nothing. (Guidance plus worked examples.)
7. **Watch for the warm "AI tint".** An orange or beige cast over everything reads as generated. Say so in Color palette or Avoid ("avoid an overall orange cast; neutral white balance"). (4+ rejections.)
8. **Match realism to the claimed capture device.** "Shot on a phone" should look like a phone photo, not stylized hyper-detail. More fidelity isn't automatically better. (1 project, 3 rounds.)
9. **Variants differ in mechanism, not color.** Change the concept, composition or medium between variants. For a pick-one round, ask for separate `image_gen` calls, one per variant. (Guidance.)
10. **Retry one failure at a time.** Restate every locked invariant and don't rewrite the concept. (Guidance.)

## Getting the result back

After the turn ends, list the newest files in `$CODEX_HOME/generated_images/<thread id>/`. To see what Codex actually sent, read the `revised_prompt` fields from the thread's rollout:

```bash
grep -o '"revised_prompt":"[^"]*"' "${CODEX_HOME:-$HOME/.codex}"/sessions/*/*/*/*<thread id>.jsonl
```

To use an image in an artifact or page, copy the file into your working folder. The generated-images folder grows large and may get cleaned up.
