import { Link as RouterLink, useRouterState } from "@tanstack/react-router"
import type { LucideIcon } from "lucide-react"
import { useEffect, useMemo, useState } from "react"

import {
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  useSidebar,
} from "@/components/ui/sidebar"

export type Item = {
  icon: LucideIcon
  title: string
  path?: "/patients" | "/patients/new" | "/admin"
  href?: string
}

interface MainProps {
  items: Item[]
  label?: string
}

export function Main({ items, label }: MainProps) {
  const { isMobile, setOpenMobile } = useSidebar()
  const router = useRouterState()
  const currentPath = router.location.pathname
  const anchorIds = useMemo(
    () =>
      items
        .map((item) => item.href?.replace(/^#/, ""))
        .filter((value): value is string => Boolean(value)),
    [items],
  )
  const [activeAnchor, setActiveAnchor] = useState(
    typeof window === "undefined" ? "" : window.location.hash,
  )

  useEffect(() => {
    if (anchorIds.length === 0) return
    const updateFromHash = () => setActiveAnchor(window.location.hash)
    window.addEventListener("hashchange", updateFromHash)

    const visible = new Map<string, number>()
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting)
            visible.set(entry.target.id, entry.intersectionRatio)
          else visible.delete(entry.target.id)
        }
        const best = [...visible.entries()].sort((a, b) => b[1] - a[1])[0]
        if (best) setActiveAnchor(`#${best[0]}`)
      },
      { rootMargin: "-18% 0px -62% 0px", threshold: [0.05, 0.25, 0.5] },
    )
    for (const id of anchorIds) {
      const element = document.getElementById(id)
      if (element) observer.observe(element)
    }
    return () => {
      observer.disconnect()
      window.removeEventListener("hashchange", updateFromHash)
    }
  }, [anchorIds])

  const handleMenuClick = (href?: string) => {
    if (href) {
      setActiveAnchor(href)
      const target = document.getElementById(href.replace(/^#/, ""))
      if (target) {
        target.classList.remove("section-target-highlight")
        requestAnimationFrame(() =>
          target.classList.add("section-target-highlight"),
        )
        window.setTimeout(
          () => target.classList.remove("section-target-highlight"),
          1_500,
        )
      }
    }
    if (isMobile) {
      setOpenMobile(false)
    }
  }

  return (
    <SidebarGroup>
      {label && <SidebarGroupLabel>{label}</SidebarGroupLabel>}
      <SidebarGroupContent>
        <SidebarMenu>
          {items.map((item) => {
            const isActive = item.path
              ? currentPath === item.path ||
                (item.path !== "/patients" &&
                  currentPath.startsWith(`${item.path}/`))
              : item.href === activeAnchor

            return (
              <SidebarMenuItem key={item.title}>
                <SidebarMenuButton
                  tooltip={item.title}
                  isActive={isActive}
                  asChild
                >
                  {item.href ? (
                    <a
                      aria-current={isActive ? "location" : undefined}
                      href={item.href}
                      onClick={() => handleMenuClick(item.href)}
                    >
                      <item.icon />
                      <span>{item.title}</span>
                    </a>
                  ) : (
                    <RouterLink
                      to={item.path ?? "/patients"}
                      onClick={() => handleMenuClick()}
                    >
                      <item.icon />
                      <span>{item.title}</span>
                    </RouterLink>
                  )}
                </SidebarMenuButton>
              </SidebarMenuItem>
            )
          })}
        </SidebarMenu>
      </SidebarGroupContent>
    </SidebarGroup>
  )
}
