import { register } from 'node:module'

register('./moduleLoader.mjs', import.meta.url)
