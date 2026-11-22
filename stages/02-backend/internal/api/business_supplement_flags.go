package api

import (
	"os"
	"strconv"
	"strings"

	"github.com/gin-gonic/gin"
)

// A paused write entry still allows users to read already committed records.
func supplementEnabled(name string) gin.HandlerFunc {
	raw, configured := os.LookupEnv(name)
	value, err := strconv.ParseBool(strings.TrimSpace(raw))
	return func(c *gin.Context) {
		if configured && (err != nil || !value) {
			fail(c, 503, 50301, "该功能暂时停用")
			return
		}
		c.Next()
	}
}
