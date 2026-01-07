import React from 'react';

export type AppLayoutProps = {
  children?: React.ReactNode;
}

export default function AppLayout(props: AppLayoutProps) {
  return (
    <div className='app-layout w-full h-full min-h-screen'>
      {props.children}
    </div>
  )
}
