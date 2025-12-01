# Architecture

The canonical current design is [`../ARCHITECTURE.md`](../ARCHITECTURE.md).
It describes the four scheduled CodeBuild commands, S3 Object Annotation
state, Confluence Automation/Rovo exchange, single-pass clip finalization,
idempotency, and crash recovery.

The earlier event-driven Lambda/SQS/Step Functions/DynamoDB design has been
removed. [`../VISION.md`](../VISION.md) remains historical product context;
[`../new.md`](../new.md) is the revised governing technical vision.

Related references:

- [Platform contracts](contracts.md)
- [Configuration](configuration.md)
- [Deployment](deployment.md)
- [Rovo Automation](../automation/README.md)
- [Replay and recovery](runbooks/replay.md)
- [Operations and limitations](operations.md)
