"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { Eye, EyeOff, Loader2, UserPlus } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { useForm } from "react-hook-form";
import { toast } from "sonner";
import { z } from "zod";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useAuthStore } from "@/store/auth";

const registerSchema = z
  .object({
    name: z.string().min(2, "Nama minimal 2 karakter.").max(80, "Nama terlalu panjang."),
    email: z.string().email("Masukkan alamat email yang valid."),
    password: z
      .string()
      .min(8, "Kata sandi minimal 8 karakter.")
      .regex(/[A-Za-z]/, "Kata sandi harus mengandung huruf.")
      .regex(/\d/, "Kata sandi harus mengandung angka."),
    confirm_password: z.string().min(1, "Konfirmasi kata sandi wajib diisi."),
    accept_terms: z.boolean().refine((value) => value === true, {
      message: "Anda harus menyetujui kebijakan privasi.",
    }),
  })
  .refine((values) => values.password === values.confirm_password, {
    message: "Konfirmasi kata sandi tidak cocok.",
    path: ["confirm_password"],
  });

type RegisterValues = z.infer<typeof registerSchema>;

export default function RegisterPage() {
  const router = useRouter();
  const { register: registerUser, user } = useAuthStore();
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const form = useForm<RegisterValues>({
    resolver: zodResolver(registerSchema),
    defaultValues: { name: "", email: "", password: "", confirm_password: "", accept_terms: true },
  });

  useEffect(() => {
    if (user) router.replace("/dashboard");
  }, [user, router]);

  async function onSubmit(values: RegisterValues) {
    setError(null);
    try {
      await registerUser(values.name, values.email, values.password);
      toast.success("Akun berhasil dibuat.");
      router.push("/dashboard");
      router.refresh();
    } catch (caught) {
      setError((caught as Error).message);
    }
  }

  return (
    <div className="container flex min-h-[calc(100vh-12rem)] items-center justify-center py-12">
      <div className="w-full max-w-md">
        <Card>
          <CardHeader className="text-center">
            <span className="mx-auto grid h-11 w-11 place-items-center rounded-xl bg-primary/10 text-primary">
              <UserPlus className="h-5 w-5" />
            </span>
            <CardTitle className="mt-3 text-2xl">Buat akun HXconvertX</CardTitle>
            <CardDescription>Riwayat lintas perangkat, profil, dan pengaturan akun.</CardDescription>
          </CardHeader>
          <CardContent>
            <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-4" noValidate>
              {error && (
                <div role="alert" className="rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive">
                  {error}
                </div>
              )}
              <div className="space-y-1.5">
                <Label htmlFor="name">Nama</Label>
                <Input id="name" autoComplete="name" placeholder="Nama Anda" {...form.register("name")} />
                {form.formState.errors.name && <p className="text-xs text-destructive">{form.formState.errors.name.message}</p>}
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="email">Email</Label>
                <Input id="email" type="email" autoComplete="email" placeholder="nama@contoh.com" {...form.register("email")} />
                {form.formState.errors.email && <p className="text-xs text-destructive">{form.formState.errors.email.message}</p>}
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="password">Kata sandi</Label>
                <div className="relative">
                  <Input
                    id="password"
                    type={showPassword ? "text" : "password"}
                    autoComplete="new-password"
                    className="pr-10"
                    {...form.register("password")}
                  />
                  <button
                    type="button"
                    onClick={() => setShowPassword((value) => !value)}
                    className="absolute right-2 top-1/2 grid h-7 w-7 -translate-y-1/2 place-items-center rounded text-muted-foreground hover:bg-muted"
                    aria-label={showPassword ? "Sembunyikan kata sandi" : "Tampilkan kata sandi"}
                  >
                    {showPassword ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                  </button>
                </div>
                <p className="text-xs text-muted-foreground">Minimal 8 karakter, mengandung huruf dan angka.</p>
                {form.formState.errors.password && (
                  <p className="text-xs text-destructive">{form.formState.errors.password.message}</p>
                )}
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="confirm_password">Konfirmasi kata sandi</Label>
                <Input
                  id="confirm_password"
                  type={showPassword ? "text" : "password"}
                  autoComplete="new-password"
                  {...form.register("confirm_password")}
                />
                {form.formState.errors.confirm_password && (
                  <p className="text-xs text-destructive">{form.formState.errors.confirm_password.message}</p>
                )}
              </div>
              <label className="flex items-start gap-2 text-xs text-muted-foreground">
                <input type="checkbox" className="mt-0.5 h-4 w-4 rounded border-input accent-primary" {...form.register("accept_terms")} />
                <span>
                  Saya menyetujui{" "}
                  <Link href="/privacy" className="text-primary hover:underline">kebijakan privasi</Link>{" "}
                  dan memahami bahwa hasil konversi disimpan sementara.
                </span>
              </label>
              {form.formState.errors.accept_terms && (
                <p className="text-xs text-destructive">{form.formState.errors.accept_terms.message}</p>
              )}
              <Button type="submit" className="w-full" disabled={form.formState.isSubmitting}>
                {form.formState.isSubmitting && <Loader2 className="h-4 w-4 animate-spin" />}
                Daftar
              </Button>
            </form>
            <p className="mt-5 text-center text-sm text-muted-foreground">
              Sudah punya akun?{" "}
              <Link href="/login" className="font-medium text-primary hover:underline">Masuk</Link>
            </p>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
