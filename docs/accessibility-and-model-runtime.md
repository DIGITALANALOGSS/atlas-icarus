# Accessibility requirements and local model runtime

## Accessibility requirements

The project owner is losing vision. Accessibility is a core product and
development-workflow requirement.

- Never require manual command typing.
- Supply complete, copy-and-paste-ready commands without terminal prompts.
- Do not require users to edit IDs, paths, tokens, or placeholders by hand.
- Minimize visual scanning and repetitive terminal-output collection.
- Reuse successful validation evidence instead of repeating completed checks.
- Group related work into bounded steps with clear success and failure output.
- Keep optional infrastructure tasks separate from the current project goal.
- Prioritize accessible interaction, including voice and assistive-technology
  workflows, as the platform develops.
- Never print credential values in diagnostics.

## Verified local runtime: 2026-10-17

- Atlas development distribution: Ubuntu-22.04 under WSL 2.
- Default WSL distribution: Ubuntu-22.04.
- Model service: atlas-qwen.service.
- Service definition: /etc/systemd/system/atlas-qwen.service.
- Source credential: /etc/atlas-qwen/model.key.
- Credentials must never be copied into this repository.
- Model alias: atlas-qwen.
- Model port: 8080.
- The systemd service is enabled.
- The model started after terminating and reopening Ubuntu-22.04.
- Authenticated generation succeeded locally and from atlas-control.
- The authenticated Atlas conversation workflow completed successfully.
- Previously saved messages remained retrievable after the WSL restart.
- Fresh generation through Atlas after restart returned RESTART_OK.

## Boundaries and unfinished work

- A full Windows reboot has not been tested.
- Windows sign-in task registration is unconfirmed and deferred.
- Docker Desktop startup at Windows sign-in has not been verified.
- Ubuntu-24.04 remains installed; no distribution migration was performed.
- Development authentication was used; this is not production security approval.
- A reproducible model-service installer remains future work.

## Existing repository validation

The repository provides: make validate-local
