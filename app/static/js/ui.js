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

  // Variant configurations
  const VARIANTS = {
    danger: {
      iconBg: "bg-red-100",
      iconColor: "text-red-600",
      buttonBg: "bg-red-600 hover:bg-red-700 focus-visible:ring-red-500",
      icon: `<svg class="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor">
        <path stroke-linecap="round" stroke-linejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z" />
      </svg>`,
    },
    warning: {
      iconBg: "bg-yellow-100",
      iconColor: "text-yellow-600",
      buttonBg:
        "bg-yellow-600 hover:bg-yellow-700 focus-visible:ring-yellow-500",
      icon: `<svg class="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor">
        <path stroke-linecap="round" stroke-linejoin="round" d="M12 9v3.75m9-.75a9 9 0 11-18 0 9 9 0 0118 0zm-9 3.75h.008v.008H12v-.008z" />
      </svg>`,
    },
    info: {
      iconBg: "bg-blue-100",
      iconColor: "text-blue-600",
      buttonBg: "bg-blue-600 hover:bg-blue-700 focus-visible:ring-blue-500",
      icon: `<svg class="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor">
        <path stroke-linecap="round" stroke-linejoin="round" d="M11.25 11.25l.041-.02a.75.75 0 011.063.852l-.708 2.836a.75.75 0 001.063.853l.041-.021M21 12a9 9 0 11-18 0 9 9 0 0118 0zm-9-3.75h.008v.008H12V8.25z" />
      </svg>`,
    },
    success: {
      iconBg: "bg-green-100",
      iconColor: "text-green-600",
      buttonBg: "bg-green-600 hover:bg-green-700 focus-visible:ring-green-500",
      icon: `<svg class="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor">
        <path stroke-linecap="round" stroke-linejoin="round" d="M9 12.75L11.25 15 15 9.75M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
      </svg>`,
    },
  };

  // Toast icon configurations
  const TOAST_ICONS = {
    success: `<svg class="h-5 w-5 text-green-400" fill="currentColor" viewBox="0 0 20 20">
      <path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clip-rule="evenodd" />
    </svg>`,
    error: `<svg class="h-5 w-5 text-red-400" fill="currentColor" viewBox="0 0 20 20">
      <path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.707 7.293a1 1 0 00-1.414 1.414L8.586 10l-1.293 1.293a1 1 0 101.414 1.414L10 11.414l1.293 1.293a1 1 0 001.414-1.414L11.414 10l1.293-1.293a1 1 0 00-1.414-1.414L10 8.586 8.707 7.293z" clip-rule="evenodd" />
    </svg>`,
    warning: `<svg class="h-5 w-5 text-yellow-400" fill="currentColor" viewBox="0 0 20 20">
      <path fill-rule="evenodd" d="M8.257 3.099c.765-1.36 2.722-1.36 3.486 0l5.58 9.92c.75 1.334-.213 2.98-1.742 2.98H4.42c-1.53 0-2.493-1.646-1.743-2.98l5.58-9.92zM11 13a1 1 0 11-2 0 1 1 0 012 0zm-1-8a1 1 0 00-1 1v3a1 1 0 002 0V6a1 1 0 00-1-1z" clip-rule="evenodd" />
    </svg>`,
    info: `<svg class="h-5 w-5 text-blue-400" fill="currentColor" viewBox="0 0 20 20">
      <path fill-rule="evenodd" d="M18 10a8 8 0 11-16 0 8 8 0 0116 0zm-7-4a1 1 0 11-2 0 1 1 0 012 0zM9 9a1 1 0 000 2v3a1 1 0 001 1h1a1 1 0 100-2v-3a1 1 0 00-1-1H9z" clip-rule="evenodd" />
    </svg>`,
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
      iconContainer.className = `mx-auto flex h-12 w-12 flex-shrink-0 items-center justify-center rounded-full sm:mx-0 sm:h-10 sm:w-10 ${variantConfig.iconBg}`;
      // Both halves come from the VARIANTS constant above; no caller input
      // reaches this markup (title/message go in via textContent).
      iconContainer.innerHTML = `<span class="${variantConfig.iconColor}">${variantConfig.icon}</span>`; // nosemgrep

      // Reset confirm button classes and apply variant
      confirmBtn.className = `inline-flex w-full justify-center rounded-lg px-4 py-2.5 text-sm font-semibold text-white shadow-sm transition-colors duration-200 sm:w-auto focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-offset-2 ${variantConfig.buttonBg}`;

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
    const icon = TOAST_ICONS[type] || TOAST_ICONS.info;

    const toastEl = document.createElement("div");
    toastEl.id = toastId;
    toastEl.className =
      "pointer-events-auto w-full max-w-sm overflow-hidden rounded-lg bg-white shadow-lg ring-1 ring-black ring-opacity-5 transform transition-all duration-300 translate-x-full opacity-0";
    // Structure is static markup; the caller's message goes in as text below,
    // so a company or channel name containing markup can never become HTML.
    toastEl.innerHTML = `
      <div class="p-4">
        <div class="flex items-start">
          <div class="flex-shrink-0" data-toast-icon></div>
          <div class="ml-3 flex-1 pt-0.5">
            <p class="text-sm font-medium text-gray-900" data-toast-message></p>
          </div>
          <div class="ml-4 flex-shrink-0">
            <button type="button" data-toast-dismiss
                    class="inline-flex rounded-md bg-white text-gray-400 hover:text-gray-500 focus:outline-none focus:ring-2 focus:ring-primary-500 focus:ring-offset-2">
              <span class="sr-only">Close</span>
              <svg class="h-5 w-5" viewBox="0 0 20 20" fill="currentColor">
                <path fill-rule="evenodd" d="M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L8.586 10 4.293 5.707a1 1 0 010-1.414z" clip-rule="evenodd" />
              </svg>
            </button>
          </div>
        </div>
      </div>
    `;

    toastEl.querySelector("[data-toast-message]").textContent = message;
    // Icons come from the TOAST_ICONS constant above, never from a caller.
    toastEl.querySelector("[data-toast-icon]").innerHTML = icon; // nosemgrep
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
    toast,
    copyToClipboard,
    // Expose internal methods for onclick handlers
    _closeModal,
    _dismissToast,
  };
})();

// Make it available globally
window.NotipusUI = NotipusUI;
