import { vi } from 'vitest'
globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
globalThis.matchMedia = vi.fn(() => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {}, dispatchEvent() {} }))
globalThis.visualViewport ||= { addEventListener() {}, removeEventListener() {}, width: 1280, height: 720 }
