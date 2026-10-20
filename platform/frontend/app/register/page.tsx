import AccountForm from '../components/AccountForm'
import { headers } from 'next/headers'
import { redirect } from 'next/navigation'
import { localDebugToken } from '@/lib/local-debug'

export default async function RegisterPage() {
  if (localDebugToken((await headers()).get('host'))) redirect('/projects')
  return <AccountForm register />
}
