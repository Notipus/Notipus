/**
 * NotipusUI - A reusable UI component library
 *
 * Provides polished, accessible UI components including:
 * - Confirmation modals (with variants: danger, warning, info, success)
 * - Toast notifications
 * - Copy to clipboard with feedback
 *
 * Usage:
 *   // Confirmation dialog
 *   const confirmed = await NotipusUI.confirm({
 *     title: 'Delete Item?',
 *     message: 'This action cannot be undone.',
 *     variant: 'danger',
 *     confirmText: 'Delete',
 *     cancelText: 'Cancel'
 *   });
 *
 *   // Or use the convenience methods
 *   const confirmed = await NotipusUI.confirmDelete('Are you sure you want to delete this?');
 *   const confirmed = await NotipusUI.confirmDisconnect('Disconnect from Slack?');
 *
 *   // Toast notifications
 *   NotipusUI.toast('Operation successful!', 'success');
 *   NotipusUI.toast('Something went wrong', 'error');
 *
 *   // Copy to clipboard
 *   NotipusUI.copyToClipboard('text to copy');
 */

const NotipusUI = (function () {
  // Private state
  let _resolvePromise = null;
  let _previouslyFocusedElement = null;

  // Variant configurations.
  //
  // Every class here is a design token from app/core/design_tokens.py, so the
  // dialog cannot drift from the components rendered server-side. Tailwind
  // scans this file (see the @source rule in src/css/main.css), which is what
  // keeps these classes in the bundle.
  //
  // Warning has no solid fill on purpose: an AA-compliant yellow goes brown and
  // reads as the brand orange, so warning confirmations use the danger button
  // and carry their meaning in the tinted icon.
  const VARIANTS = {
    danger: {
      iconBg: "bg-danger-surface",
      iconColor: "text-danger-text",
      buttonBg: "bg-danger-solid hover:bg-danger-solid-hover",
      icon: "ti-alert-triangle",
    },
    warning: {
      iconBg: "bg-warning-surface",
      iconColor: "text-warning-text",
      buttonBg: "bg-danger-solid hover:bg-danger-solid-hover",
      icon: "ti-alert-circle",
    },
    info: {
      iconBg: "bg-info-surface",
      iconColor: "text-info-text",
      buttonBg: "bg-info-solid hover:bg-info-solid-hover",
      icon: "ti-info-circle",
    },
    success: {
      iconBg: "bg-success-surface",
      iconColor: "text-success-text",
      buttonBg: "bg-success-solid hover:bg-success-solid-hover",
      icon: "ti-circle-check",
    },
  };

  // Toast icons, matched to the alert component's tone icons.
  const TOAST_ICONS = {
    success: { icon: "ti-circle-check", color: "text-success-text" },
    error: { icon: "ti-alert-circle", color: "text-danger-text" },
    warning: { icon: "ti-alert-triangle", color: "text-warning-text" },
    info: { icon: "ti-info-circle", color: "text-info-text" },
  };

  /**
   * Show a confirmation modal dialog
   * @param {Object} options - Configuration options
   * @param {string} options.title - Modal title
   * @param {string} options.message - Modal message/description
   * @param {string} [options.variant='danger'] - Visual variant: 'danger', 'warning', 'info', 'success'
   * @param {string} [options.confirmText='Confirm'] - Confirm button text
   * @param {string} [options.cancelText='Cancel'] - Cancel button text
   * @returns {Promise<boolean>} - Resolves to true if confirmed, false if cancelled
   */
  function confirm(options = {}) {
    const {
      title = "Are you sure?",
      message = "",
      variant = "danger",
      confirmText = "Confirm",
      cancelText = "Cancel",
    } = options;

    return new Promise((resolve) => {
      _resolvePromise = resolve;
      _previouslyFocusedElement = document.activeElement;

      const backdrop = document.getElementById("notipus-modal-backdrop");
      const panel = document.getElementById("notipus-modal-panel");
      const iconContainer = document.getElementById("notipus-modal-icon");
      const titleEl = document.getElementById("notipus-modal-title");
      const messageEl = document.getElementById("notipus-modal-message");
      const confirmBtn = document.getElementById("notipus-modal-confirm");
      const cancelBtn = document.getElementById("notipus-modal-cancel");

      if (!backdrop) {
        console.error("NotipusUI: Modal elements not found in DOM");
        resolve(false);
        return;
      }

      // Get variant config
      const variantConfig = VARIANTS[variant] || VARIANTS.danger;

      // Set content
      titleEl.textContent = title;
      messageEl.textContent = message;
      confirmBtn.textContent = confirmText;
      cancelBtn.textContent = cancelText;

      // Set variant styles
      iconContainer.className = `mx-auto flex h-10 w-10 flex-shrink-0 items-center justify-center rounded-control sm:mx-0 ${variantConfig.iconBg}`;
      // Both halves come from the VARIANTS constant above; no caller input
      // reaches this markup (title/message go in via textContent).
      iconContainer.innerHTML = `<i class="ti ${variantConfig.icon} text-heading ${variantConfig.iconColor}" aria-hidden="true"></i>`; // nosemgrep

      // Reset confirm button classes and apply variant
      confirmBtn.className = `inline-flex w-full items-center justify-center gap-2 rounded-control border border-transparent px-4 py-2 text-body font-medium text-content-inverse transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus focus-visible:ring-offset-2 sm:w-auto ${variantConfig.buttonBg}`;

      // Show modal with animation
      backdrop.classList.remove("hidden");
      backdrop.setAttribute("aria-hidden", "false");

      // Trigger animations
      requestAnimationFrame(() => {
        panel.classList.add("modal-enter");
        document
          .getElementById("notipus-modal-overlay")
          .classList.add("modal-overlay-enter");
      });

      // Focus the cancel button (safer default)
      setTimeout(() => cancelBtn.focus(), 50);

      // Add keyboard listener
      document.addEventListener("keydown", _handleKeydown);
    });
  }

  /**
   * Close the modal and resolve the promise
   * @param {boolean} confirmed - Whether the user confirmed
   * @private
   */
  function _closeModal(confirmed) {
    const backdrop = document.getElementById("notipus-modal-backdrop");
    const panel = document.getElementById("notipus-modal-panel");
    const overlay = document.getElementById("notipus-modal-overlay");

    if (!backdrop) return;

    // Remove animation classes and add exit animation
    panel.classList.remove("modal-enter");
    overlay.classList.remove("modal-overlay-enter");
    panel.classList.add("modal-exit");
    overlay.classList.add("modal-overlay-exit");

    // Hide after animation
    setTimeout(() => {
      backdrop.classList.add("hidden");
      backdrop.setAttribute("aria-hidden", "true");
      panel.classList.remove("modal-exit");
      overlay.classList.remove("modal-overlay-exit");

      // Restore focus
      if (_previouslyFocusedElement) {
        _previouslyFocusedElement.focus();
      }
    }, 150);

    // Remove keyboard listener
    document.removeEventListener("keydown", _handleKeydown);

    // Resolve the promise
    if (_resolvePromise) {
      _resolvePromise(confirmed);
      _resolvePromise = null;
    }
  }

  /**
   * Handle keyboard events for modal
   * @private
   */
  function _handleKeydown(event) {
    if (event.key === "Escape") {
      event.preventDefault();
      _closeModal(false);
    }

    // Trap focus within modal
    if (event.key === "Tab") {
      const confirmBtn = document.getElementById("notipus-modal-confirm");
      const cancelBtn = document.getElementById("notipus-modal-cancel");

      if (event.shiftKey) {
        if (document.activeElement === cancelBtn) {
          event.preventDefault();
          confirmBtn.focus();
        }
      } else {
        if (document.activeElement === confirmBtn) {
          event.preventDefault();
          cancelBtn.focus();
        }
      }
    }
  }

  /**
   * Convenience method for delete confirmations
   * @param {string} message - The message to display
   * @param {string} [itemName] - Optional item name for the title
   * @returns {Promise<boolean>}
   */
  function confirmDelete(message, itemName = null) {
    return confirm({
      title: itemName ? `Delete ${itemName}?` : "Delete Item?",
      message: message,
      variant: "danger",
      confirmText: "Delete",
      cancelText: "Cancel",
    });
  }

  /**
   * Convenience method for disconnect confirmations
   * @param {string} serviceName - The service being disconnected
   * @param {string} message - Additional context message
   * @returns {Promise<boolean>}
   */
  function confirmDisconnect(serviceName, message) {
    return confirm({
      title: `Disconnect ${serviceName}?`,
      message: message,
      variant: "warning",
      confirmText: "Disconnect",
      cancelText: "Keep Connected",
    });
  }

  /**
   * Convenience method for action confirmations (non-destructive)
   * @param {string} title - The action title
   * @param {string} message - The message to display
   * @param {string} [confirmText='Confirm'] - Confirm button text
   * @returns {Promise<boolean>}
   */
  function confirmAction(title, message, confirmText = "Confirm") {
    return confirm({
      title: title,
      message: message,
      variant: "info",
      confirmText: confirmText,
      cancelText: "Cancel",
    });
  }

  /**
   * Gate a form submission behind a confirmation dialog.
   *
   * Use from onsubmit so destructive forms get the styled dialog instead of the
   * browser's confirm(), which cannot be themed and looks nothing like the rest
   * of the interface:
   *
   *   <form onsubmit="return NotipusUI.confirmSubmit(this, {title: 'Remove Ada?'})">
   *
   * @param {HTMLFormElement} form - The form being submitted
   * @param {Object} options - Same options as confirm()
   * @returns {boolean} - Always false; the form is resubmitted once confirmed
   */
  function confirmSubmit(form, options = {}) {
    if (form.dataset.confirmed === "true") {
      return true;
    }
    confirm(options).then((confirmed) => {
      if (confirmed) {
        form.dataset.confirmed = "true";
        form.submit();
      }
    });
    return false;
  }

  /**
   * Show a toast notification
   * @param {string} message - The message to display
   * @param {string} [type='info'] - Toast type: 'success', 'error', 'warning', 'info'
   * @param {number} [duration=4000] - Duration in milliseconds
   */
  function toast(message, type = "info", duration = 4000) {
    const container = document.getElementById("notipus-toast-container");
    if (!container) {
      console.error("NotipusUI: Toast container not found in DOM");
      return;
    }

    const toastId = `toast-${Date.now()}`;
    const { icon, color } = TOAST_ICONS[type] || TOAST_ICONS.info;

    const toastEl = document.createElement("div");
    toastEl.id = toastId;
    toastEl.className =
      "pointer-events-auto w-full max-w-sm overflow-hidden rounded-card border border-border bg-surface shadow-overlay transform transition-all duration-300 translate-x-full opacity-0";
    // Structure is static markup — the icon class comes from the TOAST_ICONS
    // constant above and the caller's message goes in as text below, so a
    // company or channel name containing markup can never become HTML.
    toastEl.innerHTML = `
      <div class="flex items-start gap-3 p-4">
        <i class="ti ${icon} ${color} mt-0.5 flex-shrink-0 text-heading" aria-hidden="true"></i>
        <p class="min-w-0 flex-1 text-body font-medium text-content" data-toast-message></p>
        <button type="button" data-toast-dismiss
                class="flex-shrink-0 rounded-control p-1 text-content-subtle transition-colors hover:bg-surface-muted hover:text-content focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus focus-visible:ring-offset-2">
          <span class="sr-only">Dismiss</span>
          <i class="ti ti-x" aria-hidden="true"></i>
        </button>
      </div>
    `;

    toastEl.querySelector("[data-toast-message]").textContent = message;
    toastEl
      .querySelector("[data-toast-dismiss]")
      .addEventListener("click", () => _dismissToast(toastId));

    container.appendChild(toastEl);

    // Trigger enter animation
    requestAnimationFrame(() => {
      toastEl.classList.remove("translate-x-full", "opacity-0");
    });

    // Auto dismiss
    if (duration > 0) {
      setTimeout(() => _dismissToast(toastId), duration);
    }
  }

  /**
   * Dismiss a toast notification
   * @param {string} toastId - The toast element ID
   * @private
   */
  function _dismissToast(toastId) {
    const toastEl = document.getElementById(toastId);
    if (!toastEl) return;

    // Exit animation
    toastEl.classList.add("translate-x-full", "opacity-0");

    // Remove from DOM after animation
    setTimeout(() => {
      toastEl.remove();
    }, 300);
  }

  /**
   * Copy text to clipboard with toast feedback
   * @param {string} text - The text to copy
   * @param {string} [successMessage='Copied to clipboard!'] - Success message
   */
  async function copyToClipboard(text, successMessage = "Copied to clipboard!") {
    try {
      await navigator.clipboard.writeText(text);
      toast(successMessage, "success", 2000);
    } catch (err) {
      console.error("Failed to copy:", err);
      toast("Failed to copy to clipboard", "error");
    }
  }

  // Public API
  return {
    confirm,
    confirmDelete,
    confirmDisconnect,
    confirmAction,
    confirmSubmit,
    toast,
    copyToClipboard,
    // Expose internal methods for onclick handlers
    _closeModal,
    _dismissToast,
  };
})();

// Make it available globally
window.NotipusUI = NotipusUI;
