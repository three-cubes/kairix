# NOSONAR(docker:S6504): vault-agent is a sidecar that runs only at boot to
# fetch secrets from a managed-identity-authenticated Key Vault, then exits.
# It writes to a tmpfs at /run/secrets/ and never accepts user input — root
# is the expected user for the upstream azure-cli base image. Reviewed and safe.
FROM mcr.microsoft.com/azure-cli:2.73.0
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
