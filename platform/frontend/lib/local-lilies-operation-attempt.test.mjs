import assert from 'node:assert/strict'
import test from 'node:test'

import { LocalLiliesOperationAttempt } from './local-lilies-operation-attempt.ts'

test('failed retries keep one operation key until the attempt is reset', () => {
  let generated = 0
  const attempt = new LocalLiliesOperationAttempt(() => `operation-${++generated}`)

  const firstRequest = attempt.current()
  const failedRetry = attempt.current()

  assert.equal(firstRequest, 'operation-1')
  assert.equal(failedRetry, firstRequest)
  assert.equal(generated, 1)

  attempt.reset()
  assert.equal(attempt.current(), 'operation-2')
  assert.equal(generated, 2)
})

test('the attempt tracker retains only its generated idempotency key', () => {
  const attempt = new LocalLiliesOperationAttempt(() => 'non-secret-operation-key')

  assert.equal(attempt.current(), 'non-secret-operation-key')
  assert.deepEqual(Object.keys(attempt), ['idempotencyKey', 'createKey'])
  assert.equal(JSON.stringify(attempt).includes('pairing'), false)
})
