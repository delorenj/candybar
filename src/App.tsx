import { ThemeProvider } from '@/components/ThemeProvider'
import BloodbankObservability from '@/pages/Home'

function App() {
  return (
    <ThemeProvider defaultTheme="dark" storageKey="candybar-theme">
      <BloodbankObservability />
    </ThemeProvider>
  )
}

export default App
