For an ordinary source unit:

* `<relative/source/path>` — <stable purpose, responsibility, behavior, config, or artifact>; read this file for <routing reason>, <routing reason>, and <routing reason>.

For a complex orchestration or behavioral root with supplied dependency context:

* `<relative/source/path>`
  * **Purpose:** <stable responsibility or observable outcome>
  * **Behavior:** <important lifecycle, gates, and delegated work>
  * **Outputs/side effects:** <meaningful products or state changes>
  * **Boundaries:** <important stopping points or excluded outcomes>
  * **Configuration:** <important invocation or operating constraints>
  * **Implementation:** <primary routing paths or symbols>

Use only the labels that add meaningful information. Preserve important responsibility boundaries when they distinguish what the source does from what it deliberately does not do. Put observable behavior and boundaries before implementation routing. Do not replace behavior established by supplied dependency context with instructions to read another file.
