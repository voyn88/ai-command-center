import { useState } from 'react'
import Home from './screens/Home'
import Execution from './screens/Execution'
import Tasks from './screens/Tasks'
import BackgroundLayer from './components/BackgroundLayer'

type Screen = 'home' | 'execution' | 'tasks'

function fromHash(): Screen {
  if (window.location.hash === '#execution') return 'execution'
  if (window.location.hash === '#tasks') return 'tasks'
  return 'home'
}

function App() {
  const [screen, setScreen] = useState<Screen>(fromHash)
  const navigate = (next: Screen) => {
    window.location.hash = next === 'home' ? '' : next
    setScreen(next)
  }
  return (
    <>
      <BackgroundLayer />
      {screen === 'home' && <Home onNavigate={navigate} />}
      {screen === 'execution' && <Execution onNavigate={navigate} />}
      {screen === 'tasks' && <Tasks onNavigate={navigate} />}
    </>
  )
}

export default App
