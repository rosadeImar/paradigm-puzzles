# Verified compiler

Our work on Paradigm's verified compiler is already public as a pull request to the compiler
itself: [paradigmxyz/solidus#29](https://github.com/paradigmxyz/solidus/pull/29), on `record-13`,
opened on 10 November 2026. It changes fourteen compiler files (stack shuffles, stable
promotions, literals and block layout), each certified the way the pass it changes is. The
compiler is licensed under Apache-2.0 or MIT, at your choice, and the change is under the same
two licences.
