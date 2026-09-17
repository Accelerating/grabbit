import type { ReactNode } from "react";
import { XIcon } from "lucide-react";
import { Button } from "./ui/button";
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "./ui/dialog";
import { useLanguage } from "../lib/i18n";

export function CreateDialog({ title, description, onClose, children }: { title: string; description: string; onClose: () => void; children: ReactNode }) {
  const { t } = useLanguage();
  return <Dialog open onOpenChange={(open) => { if (!open) onClose(); }}>
    <DialogContent showCloseButton={false} className="create-dialog-content">
      <DialogHeader className="create-dialog-heading">
        <DialogTitle>{title}</DialogTitle>
        <DialogDescription>{description}</DialogDescription>
      </DialogHeader>
      <DialogClose asChild><Button variant="ghost" size="icon" className="create-dialog-close" aria-label={t("closeNavigation")}><XIcon /></Button></DialogClose>
      <div className="create-dialog-body">{children}</div>
    </DialogContent>
  </Dialog>;
}
