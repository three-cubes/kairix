Feature: Bedrock provider plugin pins the AWS SigV4 wire shape
  As an operator running kairix in an AWS account with Bedrock enabled
  I want the bedrock plugin to sign every outbound request with AWS
  SigV4, to address the configured region, and to translate Bedrock's
  AccessDeniedException into the same canonical AuthError that other
  providers produce
  So that callers see the same auth failure type regardless of which
  cloud provider is fronting kairix, and so a misregioned config does
  not silently fall back to the wrong Bedrock endpoint.

  Background:
    Given a wire-endpoint fixture that records every outbound request
    And the bedrock provider configured with model id "amazon.titan-embed-text-v2:0"
    And the configured credential resolver returns AWS access key, secret, and region "us-east-1"

  @happy_path
  Scenario: A Bedrock embed request is SigV4-signed and addresses the configured region
    When the operator embeds a single text via the bedrock plugin
    Then the recorded request host is "bedrock-runtime.us-east-1.amazonaws.com"
    And the recorded request header "Authorization" begins with "AWS4-HMAC-SHA256"
    And the recorded request header "Authorization" contains "Credential="
    And the recorded request header "Authorization" contains "us-east-1/bedrock/aws4_request"

  Scenario: The configured model id flows through to the Bedrock invoke URL
    When the operator embeds a single text via the bedrock plugin
    Then the recorded request path contains "/model/amazon.titan-embed-text-v2:0/invoke"

  Scenario: Region travels via a dedicated config key not via the endpoint URL
    Given the bedrock plugin is configured with region "ap-southeast-2" via the region config key
    When the operator embeds a single text via the bedrock plugin
    Then the recorded request host is "bedrock-runtime.ap-southeast-2.amazonaws.com"
    And the recorded request header "Authorization" contains "ap-southeast-2/bedrock/aws4_request"

  @error
  Scenario: Bedrock returning AccessDeniedException maps to a canonical AuthError
    Given the wire endpoint will respond with status 403 and a Bedrock AccessDeniedException body
    When the operator embeds a single text via the bedrock plugin
    Then the bedrock plugin raises a canonical AuthError
    And the error message names the configured provider as "bedrock"

  @error
  Scenario: Bedrock returning ThrottlingException maps to a canonical RateLimited error
    Given the wire endpoint will respond with status 429 and a Bedrock ThrottlingException body
    When the operator embeds a single text via the bedrock plugin
    Then the bedrock plugin raises a canonical RateLimited error
