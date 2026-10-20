// SPDX-License-Identifier: AGPL-3.0-or-later
import { Component, type ReactNode } from 'react'

// Keeps one crashing panel from blanking the whole app.
export default class ErrorBoundary extends Component<{ name: string; children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null }

  static getDerivedStateFromError(error: Error) {
    return { error }
  }

  render() {
    if (this.state.error) {
      return (
        <div className="overlay error">
          <b>{this.props.name} crashed.</b>
          <p className="muted">{this.state.error.message}</p>
          <button onClick={() => this.setState({ error: null })}>Retry</button>
        </div>
      )
    }
    return this.props.children
  }
}
