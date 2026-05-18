Feature: Operator-tunable Azure embed connection pool
  As a kairix operator running a teaming deployment
  I want to configure the embed HTTP connection pool size for my agent concurrency
  So that concurrent embed calls don't queue and serialise

  Scenario: Default pool sizing applies when operator passes no configuration
    When the operator constructs an embed client without explicit pool config
    Then the underlying HTTP pool has at most 20 connections
    And the underlying HTTP pool keeps at most 10 idle connections warm

  Scenario: Configured pool size flows through to the client
    When the operator constructs an embed client with pool size 35
    Then the underlying HTTP pool has at most 35 connections

  Scenario: Configured keepalive count flows through to the client
    When the operator constructs an embed client with keepalive 15
    Then the underlying HTTP pool keeps at most 15 idle connections warm

  Scenario: Pool size and keepalive are independently configurable
    When the operator constructs an embed client with pool size 50 and keepalive 7
    Then the underlying HTTP pool has at most 50 connections
    And the underlying HTTP pool keeps at most 7 idle connections warm
