Feature: Cross-encoder re-ranking changes top-1 for ambiguous queries
  As an operator running kairix search
  I want enabling the cross-encoder re-ranker to promote the most semantically
  relevant document to top-1, even when BM25/RRF ranks another doc higher
  So that ambiguous queries return the right answer first

  Scenario: Enabling re-rank promotes the semantic match to top-1
    Given I have two documents that both match a BM25 query but differ in semantic relevance
    When I run BM25-then-RRF fusion without re-ranking
    Then the BM25-preferred document is at top-1
    When I apply the cross-encoder re-ranker with a more specific query
    Then the semantically relevant document is now at top-1
