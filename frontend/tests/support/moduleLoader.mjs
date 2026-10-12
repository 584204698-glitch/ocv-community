// Test-only compatibility for Vite's extensionless relative JavaScript imports.
// Packages, explicit extensions and failed transitive imports keep Node semantics.
import { extname } from 'node:path'

export async function resolve(specifier, context, nextResolve) {
  try {
    return await nextResolve(specifier, context)
  } catch (error) {
    if (error.code !== 'ERR_MODULE_NOT_FOUND'
      || !/^\.{1,2}\//.test(specifier)
      || extname(specifier)
      || /[?#]/.test(specifier)) throw error
    return nextResolve(`${specifier}.js`, context)
  }
}
