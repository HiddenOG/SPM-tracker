import { Navigate, Route, Routes } from 'react-router-dom'
import Shell from './shell/Shell'
import AskPage from './pages/ask/AskPage'
import AllChats from './pages/ask/AllChats'
import DashboardPage from './pages/dashboard/DashboardPage'
import OrdersPage from './pages/orders/OrdersPage'
import DelaysPage from './pages/delays/DelaysPage'
import StockPage from './pages/stock/StockPage'
import MessagesPage from './pages/messages/MessagesPage'
import ReportsPage from './pages/reports/ReportsPage'
import QuotesPage from './pages/quotes/QuotesPage'
import QuoteForm from './pages/quotes/QuoteForm'
import PurchaseOrdersPage from './pages/pos/PurchaseOrdersPage'
import PoForm from './pages/pos/PoForm'
import TeamPage from './pages/team/TeamPage'
import SettingsPage from './pages/team/SettingsPage'
import { DataProvider } from './lib/data'
import { authHeader, goToLogin } from './lib/session'

export default function App() {
  // Signing in still happens in the classic app; come back here afterwards.
  if (!authHeader()) { goToLogin(); return null }
  return (
    <DataProvider>
      <Routes>
        <Route element={<Shell />}>
          <Route path="/dashboard" element={<DashboardPage />} />
          <Route path="/orders" element={<OrdersPage />} />
          <Route path="/delays" element={<DelaysPage />} />
          <Route path="/stock" element={<StockPage />} />
          <Route path="/messages" element={<MessagesPage />} />
          <Route path="/reports" element={<ReportsPage />} />
          <Route path="/quotes" element={<QuotesPage />} />
          <Route path="/quotes/new" element={<QuoteForm key="new" />} />
          <Route path="/quotes/:id" element={<QuoteForm />} />
          <Route path="/purchase-orders" element={<PurchaseOrdersPage />} />
          <Route path="/purchase-orders/new" element={<PoForm key="new" />} />
          <Route path="/purchase-orders/:id" element={<PoForm />} />
          <Route path="/team" element={<TeamPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          {/* One route for a new chat and a saved one, so the page isn't
              rebuilt when a new chat gets its address mid-answer. */}
          <Route path="/ask/:chatId?" element={<AskPage />} />
          <Route path="/ask/all" element={<AllChats />} />
          <Route path="*" element={<Navigate to="/dashboard" replace />} />
        </Route>
      </Routes>
    </DataProvider>
  )
}
