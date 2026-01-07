import '../css/index.css';
import AppLayout from '../components/AppLayout';
import { ThemeProvider } from '../components/ThemeProvider';

export default function RootLayout({
  children,
}: {
  children: React.ReactNode
}) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body>
        <ThemeProvider
          attribute="class"
          defaultTheme="system"
          enableSystem
          disableTransitionOnChange
        >
          <AppLayout>
           {children}
          </AppLayout>
        </ThemeProvider>
      </body>
    </html>
  )
}
