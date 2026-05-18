Feature: Layered configuration with versioned base + sparse operator overlay
  As an operator running a kairix release on a long-lived VM
  I want my host-side override file to apply ON TOP of the image-bundled defaults
  So that a new required key shipped in the image (like `provider:` in v2026.5.17a9)
  doesn't silently vanish from my running config the moment I bind-mount my overrides

  Background:
    Given an image-bundled base config that ships every required key
    And the base config declares a schema version

  @happy_path
  Scenario: Base alone resolves every required key when no overlay is set
    Given no operator overlay file is configured
    When kairix loads its configuration
    Then the resolved config has the base's `provider:` value
    And the resolved config has the base's retrieval defaults
    And no schema-version mismatch error is raised

  Scenario: Overlay overrides specific keys without dropping required ones
    Given an operator overlay that sets `retrieval.fusion_strategy: rrf`
    And the overlay does NOT declare `provider:`
    When kairix loads its configuration
    Then the resolved config has `retrieval.fusion_strategy == "rrf"` (from overlay)
    And the resolved config has the base's `provider:` value (inherited)
    And the resolved config is internally consistent

  Scenario: Overlay can override the provider plugin choice
    Given an operator overlay that sets `provider: ollama`
    When kairix loads its configuration
    Then the resolved config has `provider == "ollama"` (overlay wins)

  Scenario: Overlay collections list REPLACES base list (not concat)
    Given an operator overlay that sets `collections.shared` to a 2-item list
    And the base config ships a 7-item `collections.shared` list
    When kairix loads its configuration
    Then the resolved config's `collections.shared` has exactly 2 items
    And the items are the overlay's two items in order

  Scenario: Nested dict merge — overlay's retrieval.boosts.entity merges with base's retrieval
    Given a base config with `retrieval.fusion_strategy: bm25_primary` and `retrieval.boosts.entity.factor: 0.20`
    And an overlay that sets only `retrieval.boosts.entity.factor: 0.50`
    When kairix loads its configuration
    Then the resolved config has `retrieval.fusion_strategy == "bm25_primary"` (from base)
    And the resolved config has `retrieval.boosts.entity.factor == 0.50` (overlay wins)

  @error
  Scenario: Overlay requires a newer schema version than the base ships → startup refuses
    Given an image-bundled base config with `_schema_version: 1`
    And an operator overlay that declares `_schema_version_required_min: 2`
    When kairix loads its configuration
    Then a ConfigValidationError is raised
    And the error message mentions the version mismatch
    And the error message points the operator at the upgrade-runbook

  @legacy
  Scenario: Legacy single-file KAIRIX_CONFIG_PATH still works (no overlay declared)
    Given the operator has only `KAIRIX_CONFIG_PATH` set to a complete single file
    And no `KAIRIX_CONFIG_OVERLAY_PATH` is set
    When kairix loads its configuration
    Then the resolved config matches the single file
    And no deep-merge is performed
