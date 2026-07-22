export class LocalLiliesOperationAttempt {
  private idempotencyKey = ''
  private readonly createKey: () => string

  constructor(createKey: () => string) {
    this.createKey = createKey
  }

  current() {
    if (!this.idempotencyKey) this.idempotencyKey = this.createKey()
    return this.idempotencyKey
  }

  reset() {
    this.idempotencyKey = ''
  }
}
