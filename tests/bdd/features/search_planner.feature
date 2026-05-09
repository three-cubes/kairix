Feature: Multi-hop query planning decomposes complex questions
  As an operator running searches against the knowledge store
  I want a multi-hop question to be split into focused sub-queries
  So that hybrid retrieval covers each aspect of the question

  Scenario: A multi-hop comparison query is decomposed into multiple sub-queries
    Given a planner backed by a fake LLM that returns two sub-queries
    When the operator decomposes the multi-hop query "compare X and Y"
    Then the planner returns at least 2 sub-queries

  Scenario: A simple single-topic query passes through unchanged
    Given a planner backed by a fake LLM that returns a single sub-query
    When the operator decomposes the simple query "what is kairix"
    Then the planner returns exactly 1 sub-query

  Scenario: A failing LLM falls back to the original query
    Given a planner backed by a fake LLM that always raises
    When the operator decomposes the multi-hop query "compare X and Y"
    Then the planner returns the original query unchanged
