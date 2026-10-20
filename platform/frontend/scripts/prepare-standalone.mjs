import { cp } from 'node:fs/promises'

// Next traces server dependencies but leaves browser assets outside standalone.
// Keep the deployable directory self-contained for both Node and Docker startup.
for (const directory of ['public', '.next/static']) {
  await cp(
    new URL(`../${directory}`, import.meta.url),
    new URL(`../.next/standalone/${directory}`, import.meta.url),
    { recursive: true },
  )
}
