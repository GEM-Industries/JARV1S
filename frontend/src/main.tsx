import React from 'react'
import ReactDOM from 'react-dom/client'
import { App } from './App'
import { initClientSurface } from './runtime/clientSurface'
import { installKeyboardNavFocus } from './runtime/keyboardNavFocus'
import { installExternalLinkHandler } from './utils/openExternalUrl'
import './index.css'

initClientSurface()
installKeyboardNavFocus()
installExternalLinkHandler()

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
