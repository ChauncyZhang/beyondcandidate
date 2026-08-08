import test from "node:test";
import assert from "node:assert/strict";
import { createEmailSettingsController } from "./emailSettingsController.js";

test("loads SMTP settings without retaining the saved password mask", async () => {
  const client = { async request() { return { data: { configured: true, host: "smtp.example.test", port: 587, tls_mode: "starttls", username: "mailer", password_masked: "********", enabled: true, version: 3 } }; } };

  const settings = await createEmailSettingsController({ client }).load();

  assert.deepEqual(settings, { configured: true, host: "smtp.example.test", port: 587, tlsMode: "starttls", username: "mailer", enabled: true, version: 3 });
  assert.equal(Object.hasOwn(settings, "password"), false);
  assert.equal(Object.hasOwn(settings, "passwordMasked"), false);
});

test("saves an explicit password replacement with If-Match and never echoes it", async () => {
  const calls = [];
  const client = { async request(path, options) { calls.push({ path, options }); return { data: { configured: true, ...options.body, password_masked: "********", version: 4 } }; } };
  const controller = createEmailSettingsController({ client, idempotencyKey: () => "email-settings-key" });

  const saved = await controller.save({ host: "smtp.example.test", port: 465, tlsMode: "tls", username: "mailer", enabled: true, version: 3 }, "new-secret");

  assert.deepEqual(calls, [{ path: "/api/v1/settings/email", options: { method: "PUT", ifMatch: '"3"', idempotencyKey: "email-settings-key", body: { host: "smtp.example.test", port: 465, tls_mode: "tls", username: "mailer", enabled: true, password: "new-secret" } } }]);
  assert.equal(Object.hasOwn(saved, "password"), false);
  assert.equal(Object.hasOwn(saved, "passwordMasked"), false);
});

test("saved-configuration test is blocked while settings are dirty", async () => {
  const calls = [];
  const client = { async request(path, options) { calls.push({ path, options }); return { data: { id: "delivery-1", status: "queued" } }; } };
  const controller = createEmailSettingsController({ client, idempotencyKey: () => "email-test-key" });

  await assert.rejects(
    controller.testSavedConfiguration({ recipient: "admin@example.test", replyToEmail: "hr@example.test", replyToName: "招聘团队" }, { dirty: true }),
    (error) => error?.code === "EMAIL_SETTINGS_DIRTY",
  );
  const delivery = await controller.testSavedConfiguration({ recipient: "admin@example.test", replyToEmail: "hr@example.test", replyToName: "招聘团队" }, { dirty: false });

  assert.equal(delivery.status, "queued");
  assert.deepEqual(calls, [{ path: "/api/v1/settings/email/test", options: { method: "POST", idempotencyKey: "email-test-key", body: { recipient: "admin@example.test", reply_to_email: "hr@example.test", reply_to_name: "招聘团队" } } }]);
});
