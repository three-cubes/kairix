Feature: kairix setup CLI
  As an operator running first-time setup or scripted bootstrapping
  I want `kairix setup --help` to document the surface and `--non-interactive --json`
  to emit a config to stdout without an LLM API key
  So that I can bootstrap kairix in CI/Docker without a working credential.

  Scenario: --help documents every flag operators need
    When the operator runs the setup CLI with `--help`
    Then the setup CLI exits with status 0
    And the help output names every documented flag

  Scenario: An invalid preset is rejected by argparse
    When the operator runs the setup CLI with `--preset not-a-preset`
    Then the setup CLI exits with status 2
    And stderr names the bad preset

  Scenario: --non-interactive --json --preset emits a JSON config to stdout
    Given a temporary document root with one markdown file
    When the operator runs the setup CLI with `--non-interactive --json --preset general --path TMP`
    Then the setup CLI exits with status 0
    And the setup CLI stdout is parseable JSON
    And the JSON config has a "paths" section
    And the JSON config has a "retrieval" section
